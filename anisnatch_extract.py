"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.top
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Writes output to:           streams.json, streams_2.json, streams_3.json ...
                              (auto-splits at 3 MB per file)
- Extracts DUB streams only.
"""

import re
import json
import os
import sys
import time
import glob

# ── FILE PATHS ────────────────────────────────────────────────────
INPUT_FILE     = "inputed_urls_list.txt"          # one URL per line
PROCESSED_FILE = "already_processed_urls_list.txt" # appended after each success
OUTPUT_BASE    = "streams"                         # → streams.json, streams_2.json …
OUTPUT_EXT     = ".json"
MAX_FILE_BYTES = 3 * 1024 * 1024                  # 3 MB
# ─────────────────────────────────────────────────────────────────


# ── HELPERS: split-file management ───────────────────────────────

def all_output_files():
    """Return sorted list of existing streams*.json files."""
    pattern = OUTPUT_BASE + "*" + OUTPUT_EXT
    files = sorted(glob.glob(pattern))
    # Ensure streams.json sorts before streams_2.json etc.
    return files


def load_all_streams():
    """
    Load every existing split file into one merged dict.
    Returns: merged_dict, file_sizes {filename: bytes}
    """
    merged = {}
    sizes  = {}
    for f in all_output_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            merged.update(data)
            sizes[f] = os.path.getsize(f)
        except Exception:
            sizes[f] = 0
    return merged, sizes


def current_write_target():
    """
    Return the filename that should receive the next entry.
    Creates streams.json if nothing exists yet.
    """
    files = all_output_files()
    if not files:
        return OUTPUT_BASE + OUTPUT_EXT          # streams.json

    last = files[-1]
    if os.path.getsize(last) >= MAX_FILE_BYTES:
        # Need a new split file
        # Parse existing index from last filename
        m = re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', last)
        idx = int(m.group(1)) + 1 if m else 2
        return f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT}"

    return last


def save_entry_to_file(url, entry):
    """
    Append one entry to the correct split file, respecting the 3 MB limit.
    If the target file would exceed 3 MB after adding the entry, start a new one.
    """
    target = current_write_target()

    # Load existing content of that file
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                bucket = json.load(f)
        except Exception:
            bucket = {}
    else:
        bucket = {}

    bucket[url] = entry
    serialised = json.dumps(bucket, indent=2, ensure_ascii=False)

    # If this single file would exceed 3 MB, start a new split
    if len(serialised.encode("utf-8")) > MAX_FILE_BYTES and len(bucket) > 1:
        # Remove what we just added and save the old file as-is
        del bucket[url]
        with open(target, "w", encoding="utf-8") as f:
            json.dump(bucket, f, indent=2, ensure_ascii=False)

        # Determine new split filename
        m = re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', target)
        idx = int(m.group(1)) + 1 if m else 2
        target = f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT}"

        bucket = {url: entry}
        serialised = json.dumps(bucket, indent=2, ensure_ascii=False)

    with open(target, "w", encoding="utf-8") as f:
        f.write(serialised)

    return target


# ── HELPERS: processed-URL list ──────────────────────────────────

def load_processed_urls():
    if not os.path.isfile(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_processed(url):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# ── HELPERS: input URL list ───────────────────────────────────────

def load_input_urls():
    if not os.path.isfile(INPUT_FILE):
        print(f"[ERROR] Input file not found: {INPUT_FILE}")
        sys.exit(1)
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    print(f"[INFO] {len(urls)} URL(s) loaded from {INPUT_FILE}")
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
    """
    Click the DUB tab if present, then return (frame, iframe_src).
    Returns (None, "") if no DUB iframe found.
    """
    frame      = None
    iframe_src = ""

    # Try clicking the DUB tab/button
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

    # Prefer iframe whose src contains 'dub'
    try:
        for el in page.query_selector_all('iframe'):
            src = el.get_attribute("src") or ""
            if "dub" in src.lower():
                iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
                frame = el.content_frame()
                print(f"  [DUB] Matched dub iframe: {iframe_src}")
                break

        # Fallback: /video/def/ iframe (loaded with DUB content after tab click)
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
            print(f"  [ERROR] Navigation failed: {e}")
            browser.close()
            return None

        frame, iframe_src = find_dub_iframe(page)

        if not frame:
            print("  [ERROR] DUB iframe not found — skipping")
            browser.close()
            return None

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        html        = frame.content()
        page_title  = page.title()
        browser.close()

    data = extract_stream_data(html, iframe_src=iframe_src)
    if not any(k.startswith("stream_url_") for k in data):
        print("  [ERROR] No stream URL found in DUB iframe")
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

def main():
    # 1. Load input URLs
    input_urls = load_input_urls()

    # 2. Load already-processed URLs so we skip them
    processed = load_processed_urls()
    print(f"[INFO] {len(processed)} URL(s) already processed — will skip")

    # 3. Determine the global next serial across ALL split files
    all_streams, _ = load_all_streams()
    existing_serials = [
        v.get("serial", 0)
        for v in all_streams.values()
        if isinstance(v, dict)
    ]
    next_serial = max(existing_serials, default=0) + 1

    # 4. Filter to only unprocessed URLs
    pending = [u for u in input_urls if u not in processed]
    print(f"[INFO] {len(pending)} URL(s) pending extraction\n")

    if not pending:
        print("[INFO] Nothing to do — all URLs already processed.")
        sys.exit(0)

    ok = 0
    for url in pending:
        # If the URL was already saved in a streams file (but not in processed list),
        # reuse its serial; otherwise assign the next one.
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

    print(f"\n{'='*50}")
    print(f"Done: {ok}/{len(pending)} succeeded")
    print(f"Output files: {all_output_files()}")
    print(f"Processed log: {PROCESSED_FILE}")
    sys.exit(0 if ok == len(pending) else 1)


if __name__ == "__main__":
    main()
