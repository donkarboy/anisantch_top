"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.to
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
- Extracts DUB streams only.
- Batch size controlled by CLI arg: python anisnatch_extract.py --limit 100

HOW DUB EXTRACTION WORKS (based on real page HTML analysis):
─────────────────────────────────────────────────────────────
The page has two dropdowns in #server-option:

  #serverTypeMenu  → contains items: Soft Sub / Sub / DUB
                     each item: <div class="dropdown-item" data-type="sub|dub|soft-sub">
                     active item gets class "active"

  #streamTypeMenu  → contains ALL available servers for the selected type:
                     each item: <div class="dropdown-item"
                                      data-server="allmanga-allanime"
                                      data-source="def/7d24…/1535-10">
                     iframe URL = https://anisnatch.to/video/ + data-source

Strategy:
  1. Load the page (domcontentloaded).
  2. Click #serverTypeMenu [data-type="dub"] if DUB is not already active.
  3. Wait for #streamTypeMenu to repopulate.
  4. Read ALL data-source values from #streamTypeMenu — no iframe diving needed.
  5. Also capture the active/first iframe directly for stream-URL extraction.
─────────────────────────────────────────────────────────────
"""

import re
import json
import os
import sys
import time
import glob
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin

# ── FILE PATHS ────────────────────────────────────────────────────
INPUT_FILE     = "inputed_urls_list.txt"
PROCESSED_FILE = "already_processed_urls_list.txt"
ERROR_FILE     = "error_faced_urls_list.txt"
OUTPUT_BASE    = "streams"
OUTPUT_EXT     = ".json"
MAX_FILE_BYTES = 3 * 1024 * 1024   # 3 MB
BASE_URL       = "https://anisnatch.to"
# ─────────────────────────────────────────────────────────────────


# ── SPLIT-FILE MANAGEMENT ─────────────────────────────────────────

def all_output_files():
    base     = glob.glob(OUTPUT_BASE + OUTPUT_EXT)
    numbered = sorted(
        glob.glob(f"{OUTPUT_BASE}_*{OUTPUT_EXT}"),
        key=lambda f: int(re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f).group(1))
        if re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f) else 0
    )
    return base + numbered


def load_all_streams():
    merged = {}
    for f in all_output_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                merged.update(json.load(fh))
        except Exception:
            pass
    return merged


def current_write_target():
    files = all_output_files()
    if not files:
        return OUTPUT_BASE + OUTPUT_EXT
    last = files[-1]
    if os.path.getsize(last) >= MAX_FILE_BYTES:
        m   = re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', last)
        idx = int(m.group(1)) + 1 if m else 2
        return f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT}"
    return last


def save_entry_to_file(url, entry):
    target = current_write_target()
    bucket = {}
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                bucket = json.load(f)
        except Exception:
            bucket = {}

    bucket[url] = entry
    serialised  = json.dumps(bucket, indent=2, ensure_ascii=False)

    if len(serialised.encode("utf-8")) > MAX_FILE_BYTES and len(bucket) > 1:
        del bucket[url]
        with open(target, "w", encoding="utf-8") as f:
            json.dump(bucket, f, indent=2, ensure_ascii=False)
        m      = re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', target)
        idx    = int(m.group(1)) + 1 if m else 2
        target = f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT}"
        bucket = {url: entry}
        serialised = json.dumps(bucket, indent=2, ensure_ascii=False)

    with open(target, "w", encoding="utf-8") as f:
        f.write(serialised)
    return target


# ── PROCESSED / ERROR LOGS ───────────────────────────────────────

def load_processed_urls():
    if not os.path.isfile(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_processed(url):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def mark_error(url, reason):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}]  {url}  |  {reason}\n")


# ── INPUT URL LIST ────────────────────────────────────────────────

def load_input_urls():
    if not os.path.isfile(INPUT_FILE):
        print(f"[ERROR] Input file not found: {INPUT_FILE}")
        sys.exit(1)
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    print(f"[INFO] {len(urls)} URL(s) in {INPUT_FILE}")
    return urls


# ── DUB SELECTION ────────────────────────────────────────────────

def ensure_dub_selected(page):
    """
    Make sure DUB is the active server type.
    Returns True if DUB is (now) active, False if no DUB exists for this episode.

    The page has:
      #serverType button  → data-value="dub" when dub is active
      #serverTypeMenu     → .dropdown-item[data-type="dub"]  (may not exist if no dub)
    """
    # Wait for the server-option container to appear
    try:
        page.wait_for_selector("#server-option", timeout=15_000)
    except Exception:
        print("  [DUB] #server-option not found — page load issue")
        return False

    # Check if DUB is already the active type
    server_type_btn = page.query_selector("#serverType")
    if server_type_btn:
        current_val = server_type_btn.get_attribute("data-value") or ""
        if current_val.lower() == "dub":
            print("  [DUB] DUB already active (data-value=dub on #serverType)")
            return True

    # Dismiss the partPlayer overlay if present (it blocks all clicks)
    # Clicking it once collapses it without triggering navigation
    try:
        overlay = page.query_selector("div.partPlayer")
        if overlay:
            page.evaluate("() => { const el = document.querySelector('div.partPlayer'); if (el) el.style.pointerEvents = 'none'; }")
            print("  [DUB] Disabled partPlayer overlay via JS")
    except Exception:
        pass

    # Open the serverTypeMenu dropdown if it's not already open
    try:
        server_type_btn = page.query_selector("#serverType")
        if server_type_btn:
            page.evaluate("() => { const btn = document.querySelector('#serverType'); if (btn) btn.click(); }")
            time.sleep(0.5)
    except Exception:
        pass

    # Check if a DUB option even exists
    dub_item = page.query_selector('#serverTypeMenu .dropdown-item[data-type="dub"]')
    if not dub_item:
        print("  [DUB] No [data-type='dub'] item in #serverTypeMenu — no dub for this episode")
        return False

    # Click it — the page has a .partPlayer overlay that intercepts pointer events,
    # so we must bypass it with a JS dispatchEvent instead of a real mouse click.
    try:
        print("  [DUB] Clicking [data-type='dub'] in #serverTypeMenu …")

        # Strategy 1: JS click (bypasses any overlay completely)
        clicked = page.evaluate("""
            () => {
                const item = document.querySelector('#serverTypeMenu .dropdown-item[data-type="dub"]');
                if (!item) return false;
                item.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            }
        """)

        if not clicked:
            print("  [DUB] JS click returned false — element gone after query")
            return False

        # Wait for #streamTypeMenu to repopulate with dub servers
        time.sleep(2.5)

    except Exception as e:
        # Strategy 2: Playwright force click (skips actionability checks / overlay)
        try:
            print(f"  [DUB] JS click failed ({e}), trying force click …")
            dub_item.click(force=True)
            time.sleep(2.5)
        except Exception as e2:
            print(f"  [DUB] Click failed: {e2}")
            return False

    # Confirm dub is now active
    server_type_btn = page.query_selector("#serverType")
    if server_type_btn:
        val = server_type_btn.get_attribute("data-value") or ""
        if val.lower() == "dub":
            print("  [DUB] DUB confirmed active after click")
            return True

    # If data-value didn't update, check for .active class on the dub item
    active_dub = page.query_selector('#serverTypeMenu .dropdown-item.active[data-type="dub"]')
    if active_dub:
        print("  [DUB] DUB confirmed active via .active class")
        return True

    print("  [DUB] Could not confirm DUB selection after click")
    return False


# ── SERVER LIST EXTRACTION ────────────────────────────────────────

def extract_servers_from_dom(page):
    """
    Read all server entries from #streamTypeMenu.
    Returns list of dicts: {server, source, label, info, iframe_url}

    Each item in #streamTypeMenu looks like:
      <div class="dropdown-item [active]"
           data-server="allmanga-allanime"
           data-source="def/7d24…/1535-10">
        <span class="item-text text-title">AllAnime</span>
        <span class="item-info">MP4</span>   ← optional
      </div>
    """
    servers = []
    try:
        items = page.query_selector_all('#streamTypeMenu .dropdown-item')
        for item in items:
            server = item.get_attribute("data-server") or ""
            source = item.get_attribute("data-source") or ""
            if not source:
                continue

            label_el = item.query_selector(".item-text.text-title, .item-text")
            label    = label_el.inner_text().strip() if label_el else server

            info_el  = item.query_selector(".item-info")
            info     = info_el.inner_text().strip() if info_el else ""

            is_active = "active" in (item.get_attribute("class") or "")

            iframe_url = urljoin(BASE_URL + "/video/", source)

            servers.append({
                "server":     server,
                "source":     source,
                "label":      label,
                "info":       info,
                "active":     is_active,
                "iframe_url": iframe_url,
            })
    except Exception as e:
        print(f"  [DOM] Error reading #streamTypeMenu: {e}")

    return servers


# ── IFRAME STREAM EXTRACTION ──────────────────────────────────────

def extract_stream_from_iframe(page, iframe_url):
    """
    Navigate the iframe to iframe_url and extract .m3u8 / source URLs.
    Returns list of stream URLs found.
    """
    stream_urls = []

    try:
        iframe_el = page.query_selector('iframe#video-player')
        if not iframe_el:
            return stream_urls

        frame = iframe_el.content_frame()
        if not frame:
            return stream_urls

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=12_000)
            time.sleep(1.5)
        except Exception:
            pass

        html = frame.content()

        # Pattern 1: const source = {src: {url: "..."}}
        m = re.search(r'const\s+source\s*=\s*\{src\s*:\s*\{[^}]*url\s*:\s*["\']([^"\']+)["\']', html)
        if m:
            u = m.group(1)
            if u not in stream_urls:
                stream_urls.append(u)

        # Pattern 2: bare .m3u8 URLs
        for u in re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
            if u not in stream_urls:
                stream_urls.append(u)

        # Pattern 3: mp4 direct links
        for u in re.findall(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html):
            if u not in stream_urls:
                stream_urls.append(u)

        # Expand multi-quality master URLs
        expanded = []
        for u in stream_urls:
            expanded.append(u)
            multi = re.search(r'(https?://.+?/),(\d+p(?:,\d+p)+),(/[^\s"\'<>]+)', u)
            if multi:
                prefix, qualities, suffix = multi.group(1), multi.group(2), multi.group(3)
                for q in qualities.split(","):
                    if q:
                        variant = f"{prefix},{q}{suffix}"
                        if variant not in expanded:
                            expanded.append(variant)
        stream_urls = expanded

    except Exception as e:
        print(f"  [IFRAME] Extraction error: {e}")

    return stream_urls


# ── SKIPS / SUBTITLES EXTRACTION ─────────────────────────────────

def extract_meta_from_iframe(page):
    """Extract skips, subtitles, animeID, episodeNO from active iframe."""
    result = {}
    try:
        iframe_el = page.query_selector('iframe#video-player')
        if not iframe_el:
            return result
        frame = iframe_el.content_frame()
        if not frame:
            return result
        html = frame.content()

        anime_id_m = re.search(r"animeID\s*=\s*['\"](\d+)['\"]", html)
        episode_m  = re.search(r"episodeNO\s*=\s*['\"](\d+)['\"]", html)
        if anime_id_m and episode_m:
            result["mal_id"] = f"{anime_id_m.group(1)}/{episode_m.group(1)}"
        elif anime_id_m:
            result["mal_id"] = anime_id_m.group(1)

        m = re.search(r'skips\s*:\s*(\[.*?\])', html, re.DOTALL)
        if m:
            try:
                val = json.loads(m.group(1))
                if val:
                    result["skips"] = val
            except json.JSONDecodeError:
                pass

        m = re.search(r'subtitles\s*:\s*(\[.*?\])', html, re.DOTALL)
        if m:
            try:
                val = json.loads(m.group(1))
                if val:
                    result["subtitles"] = val
            except json.JSONDecodeError:
                pass

    except Exception as e:
        print(f"  [META] Extraction error: {e}")

    return result


# ── SINGLE URL PROCESSOR ─────────────────────────────────────────

def extract_one(watch_url, serial):
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r'/watch/(\d+)', watch_url) or [None, "?"])[1]
    episode  = (re.search(r'ep=(\d+)',     watch_url) or [None, "?"])[1]
    print(f"\n→ [#{serial}] Anime {anime_id}  Ep {episode}  |  {watch_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()

        # ── 1. Navigate ────────────────────────────────────────────
        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(2)   # let JS build the menus
        except Exception as e:
            browser.close()
            reason = f"Navigation failed: {e}"
            print(f"  [ERROR] {reason}")
            mark_error(watch_url, reason)
            return None

        # ── 2. Select DUB ─────────────────────────────────────────
        dub_ok = ensure_dub_selected(page)
        if not dub_ok:
            browser.close()
            reason = "No DUB available for this episode"
            print(f"  [SKIP] {reason}")
            mark_error(watch_url, reason)
            return None

        # ── 3. Read ALL server sources directly from DOM ───────────
        servers = extract_servers_from_dom(page)
        if not servers:
            browser.close()
            reason = "No servers found in #streamTypeMenu after DUB selection"
            print(f"  [ERROR] {reason}")
            mark_error(watch_url, reason)
            return None

        print(f"  [DOM] Found {len(servers)} DUB server(s):")
        for s in servers:
            active_tag = " ← active" if s["active"] else ""
            info_tag   = f" [{s['info']}]" if s["info"] else ""
            print(f"    {s['label']}{info_tag}  server={s['server']}{active_tag}")
            print(f"      iframe_url: {s['iframe_url']}")

        # ── 4. Extract stream URLs from the active iframe ──────────
        active_server = next((s for s in servers if s["active"]), servers[0])
        stream_urls   = extract_stream_from_iframe(page, active_server["iframe_url"])

        # ── 5. Extract skips / subtitles from iframe ───────────────
        meta = extract_meta_from_iframe(page)

        page_title = page.title()
        browser.close()

    # ── Build output entry ─────────────────────────────────────────
    title = (
        page_title.strip()
        if page_title and page_title.strip()
        else f"Anime {anime_id} – Episode {episode}"
    )

    entry = {
        "serial":  serial,
        "title":   title,
        "url":     watch_url,
        "type":    "dub",
    }

    # All iframe URLs from DOM (every available DUB server)
    entry["dub_servers"] = [
        {
            "server":     s["server"],
            "label":      s["label"],
            "info":       s["info"],
            "iframe_url": s["iframe_url"],
            "active":     s["active"],
        }
        for s in servers
    ]

    # Stream URLs extracted from the active server's iframe
    for i, u in enumerate(stream_urls, start=1):
        entry[f"stream_url_{i}"] = u

    # Fallback: use active server's iframe_url as stream_url_1 if no direct stream found
    if not stream_urls:
        entry["stream_url_1"] = active_server["iframe_url"]
        print(f"  [WARN] No direct stream URL extracted — using iframe URL as stream_url_1")

    entry.update(meta)

    n_streams = sum(1 for k in entry if k.startswith("stream_url_"))
    n_servers = len(servers)
    print(f"  ✓ serial={serial}  {n_servers} DUB server(s)  {n_streams} direct stream(s)")
    for i in range(1, n_streams + 1):
        print(f"    stream_url_{i}: {entry.get(f'stream_url_{i}', '')}")

    return entry


# ── MAIN ──────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="AniSnatch DUB stream extractor")
    parser.add_argument(
        "--limit",
        type=str,
        default="100",
        help=(
            "How many pending URLs to process this run. "
            "Choices: 2 | 20 | 50 | 100 | 250 | 500 | 1000 | 5000 | full  (default: 100)"
        ),
    )
    return parser.parse_args()


def resolve_limit(raw):
    raw = raw.strip().lower()
    if raw == "full":
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Unrecognised --limit value '{raw}', defaulting to 100")
        return 100


def main():
    args  = parse_args()
    limit = resolve_limit(args.limit)

    limit_label = "full" if limit is None else str(limit)
    print(f"[INFO] Batch limit: {limit_label} URL(s) per run\n")

    input_urls = load_input_urls()
    processed  = load_processed_urls()
    print(f"[INFO] {len(processed)} URL(s) already processed — skipping")

    all_streams      = load_all_streams()
    existing_serials = [
        v.get("serial", 0) for v in all_streams.values() if isinstance(v, dict)
    ]
    next_serial = max(existing_serials, default=0) + 1

    pending = [u for u in input_urls if u not in processed]
    print(f"[INFO] {len(pending)} URL(s) pending")

    batch = pending[:limit] if limit is not None else pending
    print(f"[INFO] Processing {len(batch)} URL(s) this run\n")

    if not batch:
        print("[INFO] Nothing to do — all URLs already processed.")
        sys.exit(0)

    ok     = 0
    errors = 0

    for url in batch:
        if url in all_streams and "serial" in all_streams[url]:
            serial = all_streams[url]["serial"]
        else:
            serial      = next_serial
            next_serial += 1

        entry = extract_one(url, serial)

        if entry:
            target = save_entry_to_file(url, entry)
            mark_processed(url)
            ok += 1
            print(f"  → Saved to {target}")
        else:
            errors += 1

    print(f"\n{'='*55}")
    print(f"Batch limit   : {limit_label}")
    print(f"Processed     : {ok} succeeded  |  {errors} failed")
    print(f"Output files  : {all_output_files()}")
    print(f"Processed log : {PROCESSED_FILE}")
    print(f"Error log     : {ERROR_FILE}")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
