"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.to
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
- Extracts DUB streams ONLY.
- Batch size controlled by CLI arg: python anisnatch_extract.py --limit 100
"""

import re
import json
import os
import sys
import time
import glob
import argparse
from datetime import datetime, timezone

# ── FILE PATHS ────────────────────────────────────────────────────
INPUT_FILE     = "inputed_urls_list.txt"
PROCESSED_FILE = "already_processed_urls_list.txt"
ERROR_FILE     = "error_faced_urls_list.txt"
OUTPUT_BASE    = "streams"
OUTPUT_EXT     = ".json"
MAX_FILE_BYTES = 3 * 1024 * 1024   # 3 MB
# ─────────────────────────────────────────────────────────────────


# ── SPLIT-FILE MANAGEMENT ─────────────────────────────────────────

def all_output_files():
    """Return sorted list of existing streams*.json files."""
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


# ── STREAM EXTRACTION ─────────────────────────────────────────────

def extract_stream_data(html, iframe_src=""):
    stream_urls = []

    src_match = re.search(r'const\s+source\s*=\s*\{src\s*:\s*(\{[^}]+\})', html)
    if src_match:
        try:
            src_data = json.loads(src_match.group(1))
            u = src_data.get("url", "")
            if u and u not in stream_urls:
                stream_urls.append(u)
        except json.JSONDecodeError:
            pass

    for u in re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
        if u not in stream_urls:
            stream_urls.append(u)

    # Expand multi-quality master URL into per-quality variants
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

    result = {}
    if iframe_src:
        result["iframe_url"] = iframe_src

    for i, u in enumerate(stream_urls, start=1):
        result[f"stream_url_{i}"] = u

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

    return result


# ── DUB IFRAME FINDER (fixed for anisnatch.to) ───────────────────
#
# How DUB works on anisnatch.to (confirmed from traffic analysis):
#
#   1.  The page renders a #serverTypeMenu dropdown containing:
#         <div class="dropdown-item" data-type="sub">Sub</div>
#         <div class="dropdown-item" data-type="dub">Dub</div>
#
#   2.  Clicking the dub item triggers the JS router which calls
#       api/loadSVs and reloads the <iframe id="video-player"> with
#       the dub stream source — the iframe src itself is /video/def/…
#       (no "dub" in the URL; the type is chosen by JS state).
#
#   3.  If no dub item exists in #serverTypeMenu the anime has no dub.
#
# Strategy:
#   • Wait for #serverTypeMenu to appear.
#   • Check for [data-type="dub"] inside it.
#   • If found → click it, wait for the iframe to reload.
#   • Grab the iframe and extract the stream from its HTML.
# ─────────────────────────────────────────────────────────────────

def find_dub_iframe(page):
    """
    Click the DUB option in the server-type dropdown and return
    (frame, iframe_src).  Returns (None, "") if no dub is available.
    """
    frame      = None
    iframe_src = ""

    # ── 1. Wait for the page to settle and the type menu to appear ──
    try:
        page.wait_for_selector("#serverTypeMenu", timeout=15_000)
    except Exception:
        print("  [DUB] #serverTypeMenu not found — page may not have loaded")

    # ── 2. Check whether a DUB option actually exists ──────────────
    dub_selector = '#serverTypeMenu .dropdown-item[data-type="dub"]'
    dub_item     = page.query_selector(dub_selector)

    if not dub_item:
        # Fallback: try the broader selectors used in the original code
        dub_item = (
            page.query_selector('[data-type="dub"]') or
            page.query_selector('.dub-btn')           or
            page.query_selector('button:has-text("DUB")') or
            page.query_selector('a:has-text("DUB")')
        )

    if not dub_item:
        print("  [DUB] No DUB option found in server-type menu — anime has no dub")
        return None, ""

    # ── 3. Open the dropdown first if needed, then click DUB ───────
    try:
        # Some themes hide the menu behind a toggle button
        toggle = page.query_selector('#serverTypeDropdown, [data-bs-toggle="dropdown"][aria-controls="serverTypeMenu"]')
        if toggle and not page.query_selector('#serverTypeMenu.show'):
            toggle.click()
            time.sleep(0.5)
    except Exception:
        pass

    try:
        print("  [DUB] Clicking DUB item in #serverTypeMenu …")
        dub_item.scroll_into_view_if_needed()
        dub_item.click()
        # Give the JS router time to reload the iframe
        time.sleep(3)
    except Exception as e:
        print(f"  [DUB] Click failed: {e}")
        return None, ""

    # ── 4. Locate the video-player iframe ──────────────────────────
    try:
        # The iframe id is always "video-player"
        iframe_el = page.wait_for_selector('iframe#video-player', timeout=10_000)
        if iframe_el:
            src = iframe_el.get_attribute("src") or ""
            iframe_src = src if src.startswith("http") else "https://anisnatch.to" + src
            frame      = iframe_el.content_frame()
            print(f"  [DUB] video-player iframe: {iframe_src}")
    except Exception as e:
        print(f"  [DUB] iframe#video-player wait failed: {e}")

    # ── 5. Final fallback: any /video/def/ iframe ──────────────────
    if not frame:
        try:
            for el in page.query_selector_all('iframe'):
                src = el.get_attribute("src") or ""
                if "/video/def/" in src:
                    iframe_src = src if src.startswith("http") else "https://anisnatch.to" + src
                    frame      = el.content_frame()
                    print(f"  [DUB] Fallback iframe (/video/def/): {iframe_src}")
                    break
        except Exception as e:
            print(f"  [DUB] Fallback iframe search failed: {e}")

    return frame, iframe_src


# ── SINGLE URL PROCESSOR ─────────────────────────────────────────

def extract_one(watch_url, serial):
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r'/watch/(\d+)', watch_url) or [None, "?"])[1]
    episode  = (re.search(r'ep=(\d+)',     watch_url) or [None, "?"])[1]
    print(f"\n→ [#{serial}] Anime {anime_id}  Ep {episode}  |  {watch_url}")

    error_reason = None

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

        # ── Navigate ───────────────────────────────────────────────
        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
            # Extra settle time so JS can build the server-type menu
            time.sleep(2)
        except Exception as e:
            error_reason = f"Navigation failed: {e}"
            print(f"  [ERROR] {error_reason}")
            browser.close()
            mark_error(watch_url, error_reason)
            return None

        # ── Find and click DUB ────────────────────────────────────
        frame, iframe_src = find_dub_iframe(page)

        if not frame:
            error_reason = "DUB iframe not found (no dub available or page error)"
            print(f"  [ERROR] {error_reason} — skipping")
            browser.close()
            mark_error(watch_url, error_reason)
            return None

        # ── Wait for iframe content ────────────────────────────────
        try:
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        html       = frame.content()
        page_title = page.title()
        browser.close()

    # ── Extract stream data from iframe HTML ───────────────────────
    data = extract_stream_data(html, iframe_src=iframe_src)
    if not any(k.startswith("stream_url_") for k in data):
        error_reason = "No stream URL found in DUB iframe"
        print(f"  [ERROR] {error_reason}")
        mark_error(watch_url, error_reason)
        return None

    title = (
        page_title.strip()
        if page_title and page_title.strip()
        else f"Anime {anime_id} – Episode {episode}"
    )

    entry = {"serial": serial, "title": title, "url": watch_url}
    entry.update(data)

    n = sum(1 for k in entry if k.startswith("stream_url_"))
    print(f"  ✓ serial={serial}  {n} DUB stream(s) found")
    for i in range(1, n + 1):
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
