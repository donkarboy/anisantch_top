"""
anisnatch_extract.py — Automated Stream URL Extractor for anisnatch.top
========================================================================

GitHub Actions compatible.  Reads one or more watch URLs, extracts the
HLS stream URL for each, and writes / merges results into streams.json.

OUTPUT FORMAT (streams.json)
─────────────────────────────
{
  "https://anisnatch.top/watch/1735?ep=250": {
    "title": "Anime Title – Episode 250",
    "stream_url_1": "https://cdn.example.com/hls/master.m3u8",
    "stream_url_2": "...",        ← only present when multiple sources found
    "thumbnails": "...",
    "skips": [...],
    "subtitles": [...],
    "extracted_at": "2025-01-01T00:00:00Z"
  },
  ...
}

CLI USAGE
─────────
  # Single URL (hardcode style):
  python anisnatch_extract.py --url "https://anisnatch.top/watch/1735?ep=250"

  # File of URLs (one per line, # = comment):
  python anisnatch_extract.py --url-file urls.txt

  # Override output file (default: streams.json in repo root):
  python anisnatch_extract.py --url-file urls.txt --output streams.json

REQUIREMENTS
────────────
  pip install playwright
  playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone


# ════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════

def parse_watch_url(url: str):
    """Return (anime_id, episode) from a watch URL, or (None, None)."""
    m = re.search(r'/watch/(\d+)[^?]*\?.*ep=(\d+)', url)
    if not m:
        m = re.search(r'/watch/(\d+)\?ep=(\d+)', url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def extract_stream_data(html: str) -> dict:
    """
    Parse the player-page HTML and return a dict with all found stream
    URLs keyed as stream_url_1, stream_url_2, … plus metadata.
    """
    result: dict = {}
    stream_urls: list[str] = []

    # ── Primary: const source = {src: {"url":"...","type":"..."}, ...} ──
    src_match = re.search(r'const\s+source\s*=\s*\{src\s*:\s*(\{[^}]+\})', html)
    if src_match:
        try:
            src_data = json.loads(src_match.group(1))
            url = src_data.get("url", "")
            if url:
                stream_urls.append(url)
                result["stream_type"] = src_data.get("type", "hls")
        except json.JSONDecodeError:
            pass

    # ── Fallback: all m3u8 URLs in the page ──────────────────────────
    for raw_url in re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
        if raw_url not in stream_urls:
            stream_urls.append(raw_url)

    # Store as stream_url_1, stream_url_2, …
    for i, u in enumerate(stream_urls, start=1):
        result[f"stream_url_{i}"] = u

    # ── Metadata ─────────────────────────────────────────────────────
    ep_match = re.search(r"episodeNO\s*=\s*['\"](\d+)['\"]", html)
    if ep_match:
        result["episode"] = ep_match.group(1)

    id_match = re.search(r"animeID\s*=\s*['\"](\d+)['\"]", html)
    if id_match:
        result["anime_id"] = id_match.group(1)

    site_match = re.search(r"siteName\s*=\s*['\"](\w+)['\"]", html)
    if site_match:
        result["site_name"] = site_match.group(1)

    thumb_match = re.search(r"thumbnails\s*:\s*['\"]([^'\"]+)['\"]", html)
    if thumb_match:
        result["thumbnails"] = thumb_match.group(1)

    skips_match = re.search(r'skips\s*:\s*(\[.*?\])', html, re.DOTALL)
    if skips_match:
        try:
            result["skips"] = json.loads(skips_match.group(1))
        except json.JSONDecodeError:
            pass

    subs_match = re.search(r'subtitles\s*:\s*(\[.*?\])', html, re.DOTALL)
    if subs_match:
        try:
            subs = json.loads(subs_match.group(1))
            if subs:
                result["subtitles"] = subs
        except json.JSONDecodeError:
            pass

    return result


# ════════════════════════════════════════════════════════════════════
#  BROWSER EXTRACTION
# ════════════════════════════════════════════════════════════════════

def extract_one(watch_url: str) -> dict | None:
    """
    Use a Playwright headless browser to extract stream data for a
    single episode URL.  Returns a dict on success, None on failure.
    """
    anime_id, episode = parse_watch_url(watch_url)
    if not anime_id:
        print(f"  [SKIP] Cannot parse URL: {watch_url}")
        return None

    print(f"\n{'─'*68}")
    print(f"  URL     : {watch_url}")
    print(f"  Anime   : {anime_id}  Episode: {episode}")
    print(f"{'─'*68}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [ERROR] Playwright not installed — run: pip install playwright")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        # ── Navigate ─────────────────────────────────────────────────
        print("  [1/3] Loading watch page…")
        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"  [ERROR] Navigation failed: {exc}")
            browser.close()
            return None

        # ── Wait for iframe ──────────────────────────────────────────
        print("  [2/3] Waiting for JS to inject iframe (hex token)…")
        iframe_src = None
        target_frame = None
        try:
            el = page.wait_for_selector('iframe[src*="/video/def/"]', timeout=45_000)
            if el:
                iframe_src = el.get_attribute("src") or ""
                target_frame = el.content_frame()
        except Exception:
            pass

        if not iframe_src or not target_frame:
            print("  [ERROR] Video iframe not found after 45 s")
            # Save debug snapshot
            _save_debug(page.content(), f"debug_watch_{anime_id}_ep{episode}.html")
            browser.close()
            return None

        if not iframe_src.startswith("http"):
            iframe_src = "https://anisnatch.top" + iframe_src

        print(f"  [2/3] ✓ Iframe: {iframe_src[:72]}…")

        # ── Extract from player iframe ────────────────────────────────
        print("  [3/3] Extracting stream URL from player iframe…")
        try:
            target_frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        player_html = target_frame.content()
        browser.close()

    # ── Parse HTML ────────────────────────────────────────────────────
    data = extract_stream_data(player_html)

    if not any(k.startswith("stream_url_") for k in data):
        if "VIDEO NOT FOUND" in player_html:
            print("  [ERROR] Server says VIDEO NOT FOUND for this episode")
        else:
            print("  [ERROR] No stream URL found in player HTML")
            _save_debug(player_html, f"debug_player_{anime_id}_ep{episode}.html")
        return None

    # ── Build title string ────────────────────────────────────────────
    # Best-effort title from page title tag (not in iframe, so use watch_url info)
    title = f"Anime {anime_id} – Episode {episode}"
    data["title"] = title
    data["extracted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["iframe_url"] = iframe_src

    # Log success
    for k, v in data.items():
        if k.startswith("stream_url_"):
            print(f"  ✓ {k}: {v}")

    return data


def _save_debug(content: str, filename: str):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  [DEBUG] Saved: {path}")


# ════════════════════════════════════════════════════════════════════
#  JSON MERGE & SAVE
# ════════════════════════════════════════════════════════════════════

def load_existing(output_path: str) -> dict:
    if os.path.isfile(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_streams(output_path: str, streams: dict):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(streams, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved → {output_path}  ({len(streams)} entries)")


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Extract HLS stream URLs from anisnatch.top"
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--url",
        metavar="URL",
        help="Single watch URL, e.g. https://anisnatch.top/watch/1735?ep=250",
    )
    group.add_argument(
        "--url-file",
        metavar="FILE",
        help="Path to a text file with one watch URL per line (# = comment).",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        default="streams.json",
        help="Output JSON file path (default: streams.json)",
    )
    return p.parse_args()


def main():
    args = parse_args()

    # ── Resolve URL list ──────────────────────────────────────────────
    if args.url:
        urls = [args.url.strip()]
    else:
        if not os.path.isfile(args.url_file):
            print(f"[ERROR] URL file not found: {args.url_file}")
            sys.exit(1)
        with open(args.url_file, "r", encoding="utf-8") as f:
            urls = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

    if not urls:
        print("[ERROR] No URLs to process.")
        sys.exit(1)

    print(f"\n{'='*68}")
    print(f"  anisnatch.top — Stream URL Extractor (GitHub Actions ready)")
    print(f"{'='*68}")
    print(f"  Processing {len(urls)} URL(s)  →  {args.output}")

    # ── Load existing data so we can merge / update ───────────────────
    streams = load_existing(args.output)

    # ── Process each URL ─────────────────────────────────────────────
    success = 0
    for url in urls:
        data = extract_one(url)
        if data:
            # The watch URL is the top-level key
            streams[url] = data
            success += 1

    # ── Save merged output ────────────────────────────────────────────
    if success:
        save_streams(args.output, streams)
    else:
        print("\n  [WARN] No successful extractions — streams.json not updated.")

    print(f"\n  Done: {success}/{len(urls)} succeeded.\n")
    sys.exit(0 if success == len(urls) else 1)


if __name__ == "__main__":
    main()