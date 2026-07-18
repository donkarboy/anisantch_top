"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.top
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
- Extracts DUB streams only.
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


# ── AUTO-INIT REQUIRED FILES ─────────────────────────────────────

def init_files():
    """
    Create any missing support files on first run so the repo always has them.
    Never overwrites existing content.
    """
    stubs = {
        PROCESSED_FILE: (
            "# already_processed_urls_list.txt\n"
            "# Auto-managed — one successfully processed URL per line.\n"
            "# Do NOT edit manually.\n"
        ),
        ERROR_FILE: (
            "# error_faced_urls_list.txt\n"
            "# Auto-managed — format: [YYYY-MM-DD HH:MM UTC]  <url>  |  <reason>\n"
            "# Do NOT edit manually.\n"
        ),
    }
    for path, header in stubs.items():
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)
            print(f"[INIT] Created {path}")
        else:
            print(f"[INIT] Found   {path}  ✓")


# ── SPLIT-FILE MANAGEMENT ─────────────────────────────────────────

def all_output_files():
    """Return sorted list of existing streams*.json files."""
    base    = glob.glob(OUTPUT_BASE + OUTPUT_EXT)
    numbered = sorted(
        glob.glob(f"{OUTPUT_BASE}_*{OUTPUT_EXT}"),
        key=lambda f: int(re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f).group(1))
        if re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f) else 0
    )
    return base + numbered


def load_all_streams():
    """Load every split file into one merged dict."""
    merged = {}
    for f in all_output_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                merged.update(json.load(fh))
        except Exception:
            pass
    return merged


def current_write_target():
    """Return filename that should receive the next entry."""
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
    """Append one entry to the correct split file; start new file if ≥ 3 MB."""
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

    # If adding this entry pushes the file over 3 MB, close the current file
    # and open a new split (only when the bucket already had other entries)
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
    """Append a timestamped error entry to error_faced_urls_list.txt."""
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

    # Expand multi-quality master into per-quality variants
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


def find_dub_iframe(page):
    frame      = None
    iframe_src = ""

    try:
        dub_btn = (
            page.query_selector('text=DUB') or
            page.query_selector('[data-type="dub"]') or
            page.query_selector('.dub-btn') or
            page.query_selector('button:has-text("DUB")') or
            page.query_selector('a:has-text("DUB")')
        )
        if dub_btn:
            print("  [DUB] Found DUB tab — clicking...")
            dub_btn.click()
            time.sleep(2)
    except Exception as e:
        print(f"  [DUB] Tab click skipped: {e}")

    try:
        for el in page.query_selector_all('iframe'):
            src = el.get_attribute("src") or ""
            if "dub" in src.lower():
                iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
                frame = el.content_frame()
                print(f"  [DUB] Matched dub iframe: {iframe_src}")
                break

        if not frame:
            el = page.query_selector('iframe[src*="/video/def/"]')
            if el:
                src = el.get_attribute("src") or ""
                iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
                frame = el.content_frame()
                print(f"  [DUB] Using default iframe (post-click): {iframe_src}")
    except Exception as e:
        print(f"  [DUB] iframe search error: {e}")

    return frame, iframe_src


def extract_one(watch_url, serial):
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r'/watch/(\d+)', watch_url) or [None, "?"])[1]
    episode  = (re.search(r'ep=(\d+)',     watch_url) or [None, "?"])[1]
    print(f"\n→ [#{serial}] Anime {anime_id}  Ep {episode}  |  {watch_url}")

    error_reason = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()

        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            error_reason = f"Navigation failed: {e}"
            print(f"  [ERROR] {error_reason}")
            browser.close()
            mark_error(watch_url, error_reason)
            return None

        frame, iframe_src = find_dub_iframe(page)

        if not frame:
            error_reason = "DUB iframe not found"
            print(f"  [ERROR] {error_reason} — skipping")
            browser.close()
            mark_error(watch_url, error_reason)
            return None

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        html       = frame.content()
        page_title = page.title()
        browser.close()

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
        return None   # None = no limit → process everything
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Unrecognised --limit value '{raw}', defaulting to 100")
        return 100


def main():
    args  = parse_args()
    limit = resolve_limit(args.limit)

    # ── Auto-create missing support files before anything else ──
    print("[INIT] Checking required files...")
    init_files()
    print()

    limit_label = "full" if limit is None else str(limit)
    print(f"[INFO] Batch limit: {limit_label} URL(s) per run\n")

    # Load inputs
    input_urls = load_input_urls()
    processed  = load_processed_urls()
    print(f"[INFO] {len(processed)} URL(s) already processed — skipping")

    # Global serial counter across all split files
    all_streams   = load_all_streams()
    existing_serials = [
        v.get("serial", 0) for v in all_streams.values() if isinstance(v, dict)
    ]
    next_serial = max(existing_serials, default=0) + 1

    # Pending = input minus already processed
    pending = [u for u in input_urls if u not in processed]
    print(f"[INFO] {len(pending)} URL(s) pending")

    # Apply batch limit
    if limit is not None:
        batch = pending[:limit]
    else:
        batch = pending

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
            mark_processed(url)           # ✓ save to already_processed_urls_list.txt
            ok += 1
            print(f"  → Saved to {target}")
        else:
            # error already logged inside extract_one via mark_error()
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
