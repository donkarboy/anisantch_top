"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.top
- Reads input URLs from:      inputed_urls_list.txt  (auto-splits at 5 000 URLs)
- Skips already-done URLs in: already_processed_urls_list.txt (auto-splits at 5 000 URLs)
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
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

# ── FILE PATHS & LIMITS ───────────────────────────────────────────
INPUT_BASE     = "inputed_urls_list"           # → inputed_urls_list.txt, inputed_urls_list_2.txt …
PROCESSED_BASE = "already_processed_urls_list" # → already_processed_urls_list.txt, _2.txt …
ERROR_FILE     = "error_faced_urls_list.txt"   # single file (errors stay manageable)
OUTPUT_BASE    = "streams"
OUTPUT_EXT     = ".json"
TXT_EXT        = ".txt"
MAX_JSON_BYTES = 3 * 1024 * 1024  # 3 MB  — JSON split threshold
MAX_TXT_URLS   = 5_000            # 5 000 URLs per .txt split file

# Primary filenames (split files are _2, _3 …)
INPUT_FILE     = INPUT_BASE     + TXT_EXT
PROCESSED_FILE = PROCESSED_BASE + TXT_EXT
# ─────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════
#  TXT SPLIT-FILE HELPERS  (shared by input + processed lists)
# ══════════════════════════════════════════════════════════════════

def _txt_files(base):
    """
    Return sorted list of all split files for a given base name.
    e.g. base="inputed_urls_list" →
         ["inputed_urls_list.txt", "inputed_urls_list_2.txt", ...]
    """
    primary  = base + TXT_EXT
    numbered = sorted(
        glob.glob(f"{base}_*{TXT_EXT}"),
        key=lambda f: int(m.group(1))
        if (m := re.search(r'_(\d+)' + re.escape(TXT_EXT) + r'$', f))
        else 0
    )
    result = []
    if os.path.isfile(primary):
        result.append(primary)
    result.extend(numbered)
    return result


def _load_txt_urls(base):
    """
    Load every URL from all split files for `base`.
    Returns a list (preserves order, deduplicates).
    """
    seen = set()
    urls = []
    for path in _txt_files(base):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                u = line.strip()
                if u and not u.startswith("#") and u not in seen:
                    seen.add(u)
                    urls.append(u)
    return urls


def _count_txt_urls(path):
    """Count non-blank, non-comment lines in a txt file."""
    if not os.path.isfile(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip() and not ln.startswith("#"))


def _next_txt_write_target(base):
    """
    Return the file path that should receive the next URL append.
    Creates a new split file when the current last file hits MAX_TXT_URLS.
    """
    files = _txt_files(base)
    if not files:
        return base + TXT_EXT   # primary file doesn't exist yet

    last = files[-1]
    if _count_txt_urls(last) >= MAX_TXT_URLS:
        m   = re.search(r'_(\d+)' + re.escape(TXT_EXT) + r'$', last)
        idx = int(m.group(1)) + 1 if m else 2
        new = f"{base}_{idx}{TXT_EXT}"
        print(f"[SPLIT] {last} hit {MAX_TXT_URLS} URLs → opening {new}")
        return new
    return last


def _append_url_to_txt(base, url):
    """Append one URL to the correct split file, splitting if needed."""
    target = _next_txt_write_target(base)
    with open(target, "a", encoding="utf-8") as f:
        f.write(url + "\n")
    return target


def _split_existing_txt_if_needed(base):
    """
    On startup, check whether the primary (or any) txt file exceeds
    MAX_TXT_URLS and redistribute URLs into correctly-sized splits.
    Runs once per startup; safe to call repeatedly.
    """
    all_urls = _load_txt_urls(base)
    if len(all_urls) <= MAX_TXT_URLS:
        return   # nothing to do

    print(f"[SPLIT] {base}* has {len(all_urls)} URLs — redistributing into {MAX_TXT_URLS}-URL chunks …")

    # Wipe all existing split files for this base
    for path in _txt_files(base):
        os.remove(path)

    # Rewrite in chunks
    chunks = [all_urls[i:i + MAX_TXT_URLS] for i in range(0, len(all_urls), MAX_TXT_URLS)]
    for idx, chunk in enumerate(chunks):
        path = base + TXT_EXT if idx == 0 else f"{base}_{idx + 1}{TXT_EXT}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {os.path.basename(path)} — auto-managed\n")
            for u in chunk:
                f.write(u + "\n")
        print(f"  → {path}  ({len(chunk)} URLs)")


# ══════════════════════════════════════════════════════════════════
#  AUTO-INIT REQUIRED FILES
# ══════════════════════════════════════════════════════════════════

def init_files():
    """
    Create any missing support files on first run.
    Never overwrites existing content.
    """
    stubs = {
        PROCESSED_FILE: (
            "# already_processed_urls_list.txt\n"
            "# Auto-managed — one successfully processed URL per line.\n"
            "# Auto-splits into _2.txt, _3.txt … at 5 000 URLs each.\n"
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

    # Also verify input file exists
    if not os.path.isfile(INPUT_FILE):
        with open(INPUT_FILE, "w", encoding="utf-8") as f:
            f.write(
                "# inputed_urls_list.txt\n"
                "# Add one AniSnatch watch URL per line.\n"
                "# Auto-splits into _2.txt, _3.txt … at 5 000 URLs each.\n"
            )
        print(f"[INIT] Created {INPUT_FILE}  (empty — add your URLs)")
    else:
        print(f"[INIT] Found   {INPUT_FILE}  ✓")


# ══════════════════════════════════════════════════════════════════
#  JSON SPLIT-FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def all_json_files():
    """Return sorted list of existing streams*.json files."""
    base     = glob.glob(OUTPUT_BASE + OUTPUT_EXT)
    numbered = sorted(
        glob.glob(f"{OUTPUT_BASE}_*{OUTPUT_EXT}"),
        key=lambda f: int(m.group(1))
        if (m := re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f))
        else 0
    )
    return base + numbered


def load_all_streams():
    """Load every JSON split file into one merged dict."""
    merged = {}
    for f in all_json_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                merged.update(json.load(fh))
        except Exception:
            pass
    return merged


def _current_json_write_target():
    files = all_json_files()
    if not files:
        return OUTPUT_BASE + OUTPUT_EXT
    last = files[-1]
    if os.path.getsize(last) >= MAX_JSON_BYTES:
        m   = re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', last)
        idx = int(m.group(1)) + 1 if m else 2
        return f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT}"
    return last


def save_entry_to_file(url, entry):
    """Append one entry to the correct JSON split; open new file if ≥ 3 MB."""
    target = _current_json_write_target()

    bucket = {}
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                bucket = json.load(f)
        except Exception:
            bucket = {}

    bucket[url] = entry
    serialised  = json.dumps(bucket, indent=2, ensure_ascii=False)

    if len(serialised.encode("utf-8")) > MAX_JSON_BYTES and len(bucket) > 1:
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


# ══════════════════════════════════════════════════════════════════
#  PROCESSED / ERROR LOGS
# ══════════════════════════════════════════════════════════════════

def load_processed_urls():
    """Load every URL from all already_processed_urls_list*.txt files."""
    return set(_load_txt_urls(PROCESSED_BASE))


def mark_processed(url):
    """Append URL to the correct processed split file."""
    _append_url_to_txt(PROCESSED_BASE, url)


def mark_error(url, reason):
    """Append a timestamped error line to error_faced_urls_list.txt."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}]  {url}  |  {reason}\n")


# ══════════════════════════════════════════════════════════════════
#  INPUT URL LIST
# ══════════════════════════════════════════════════════════════════

def load_input_urls():
    """Load every URL from all inputed_urls_list*.txt files."""
    if not _txt_files(INPUT_BASE):
        print(f"[ERROR] Input file not found: {INPUT_FILE}")
        sys.exit(1)
    urls = _load_txt_urls(INPUT_BASE)
    files = _txt_files(INPUT_BASE)
    print(f"[INFO] {len(urls)} URL(s) loaded from {len(files)} input file(s): {files}")
    return urls


# ══════════════════════════════════════════════════════════════════
#  STREAM EXTRACTION
# ══════════════════════════════════════════════════════════════════

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


def find_iframe(page):
    frame      = None
    iframe_src = ""

    try:
        el = page.wait_for_selector('iframe[src*="/video/def/"]', timeout=45_000)
        if el:
            src = el.get_attribute("src") or ""
            iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
            frame = el.content_frame()
            print(f"  [iframe] Found: {iframe_src}")
    except Exception as e:
        print(f"  [iframe] Not found: {e}")

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

        frame, iframe_src = find_iframe(page)

        if not frame:
            error_reason = "iframe not found"
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
        error_reason = "No stream URL found in iframe"
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
    print(f"  ✓ serial={serial}  {n} stream(s) found")
    for i in range(1, n + 1):
        print(f"    stream_url_{i}: {entry.get(f'stream_url_{i}', '')}")

    return entry


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="AniSnatch stream extractor")
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

    # ── 1. Auto-create missing support files ──
    print("[INIT] Checking required files...")
    init_files()
    print()

    # ── 2. Auto-split oversized txt files if needed ──
    print("[SPLIT] Checking txt file sizes...")
    _split_existing_txt_if_needed(INPUT_BASE)
    _split_existing_txt_if_needed(PROCESSED_BASE)
    print()

    limit_label = "full" if limit is None else str(limit)
    print(f"[INFO] Batch limit : {limit_label} URL(s) per run")

    # ── 3. Load all input URLs (across all input split files) ──
    input_urls = load_input_urls()

    # ── 4. Load already-processed URLs (across all processed split files) ──
    processed = load_processed_urls()
    proc_files = _txt_files(PROCESSED_BASE)
    print(f"[INFO] {len(processed)} URL(s) already processed "
          f"across {len(proc_files)} file(s): {proc_files}")

    # ── 5. Global serial counter across all JSON split files ──
    all_streams      = load_all_streams()
    existing_serials = [v.get("serial", 0) for v in all_streams.values() if isinstance(v, dict)]
    next_serial      = max(existing_serials, default=0) + 1

    # ── 6. Build pending list & apply batch limit ──
    pending = [u for u in input_urls if u not in processed]
    print(f"[INFO] {len(pending)} URL(s) pending")

    batch = pending if limit is None else pending[:limit]
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
            errors += 1   # error already logged via mark_error() inside extract_one

    print(f"\n{'='*55}")
    print(f"Batch limit    : {limit_label}")
    print(f"Succeeded      : {ok}")
    print(f"Failed         : {errors}")
    print(f"JSON files     : {all_json_files()}")
    print(f"Input files    : {_txt_files(INPUT_BASE)}")
    print(f"Processed files: {_txt_files(PROCESSED_BASE)}")
    print(f"Error log      : {ERROR_FILE}")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
