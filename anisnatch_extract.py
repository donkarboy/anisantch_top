"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.top
"""

import re
import json
import os
import sys
import time

# ── HARDCODED URLS — add as many as you want ──────────────────────
URLS = [
    "https://anisnatch.top/watch/1735?ep=250",
    # "https://anisnatch.top/watch/1735?ep=251",
]
# ─────────────────────────────────────────────────────────────────

OUTPUT_FILE = "streams.json"


def extract_stream_data(html):
    stream_urls = []

    src_match = re.search(r'const\s+source\s*=\s*\{src\s*:\s*(\{[^}]+\})', html)
    if src_match:
        try:
            src_data = json.loads(src_match.group(1))
            u = src_data.get("url", "")
            if u:
                stream_urls.append(u)
        except json.JSONDecodeError:
            pass

    for u in re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
        if u not in stream_urls:
            stream_urls.append(u)

    result = {}

    # stream URLs: stream_url_1, stream_url_2, ...
    for i, u in enumerate(stream_urls, start=1):
        result[f"stream_url_{i}"] = u

    result["found_streams"] = len(stream_urls)

    for pat, key in [
        (r"episodeNO\s*=\s*['\"](\d+)['\"]", "episode"),
        (r"animeID\s*=\s*['\"](\d+)['\"]",   "mal_id"),   # counted as MAL ID
        (r"siteName\s*=\s*['\"](\w+)['\"]",   "site_name"),
        (r"thumbnails\s*:\s*['\"]([^'\"]+)['\"]", "thumbnails"),
    ]:
        m = re.search(pat, html)
        if m:
            result[key] = m.group(1)

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


def extract_one(watch_url):
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r'/watch/(\d+)', watch_url) or [None, "?"])[1]
    episode  = (re.search(r'ep=(\d+)',     watch_url) or [None, "?"])[1]
    print(f"\n→ Anime {anime_id}  Ep {episode}  |  {watch_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()

        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  [ERROR] Navigation: {e}")
            browser.close()
            return None

        frame = None
        iframe_src = ""
        try:
            el = page.wait_for_selector('iframe[src*="/video/def/"]', timeout=45_000)
            if el:
                iframe_src = el.get_attribute("src") or ""
                frame = el.content_frame()
        except Exception:
            pass

        if not frame:
            print("  [ERROR] iframe not found")
            browser.close()
            return None

        if not iframe_src.startswith("http"):
            iframe_src = "https://anisnatch.top" + iframe_src

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        html = frame.content()

        # Try to grab page title for anime name
        page_title = page.title()

        browser.close()

    data = extract_stream_data(html)
    if not any(k.startswith("stream_url_") for k in data):
        print("  [ERROR] No stream URL found")
        return None

    # Build clean title: prefer page title, fallback to generic
    if page_title and page_title.strip():
        title = page_title.strip()
    else:
        title = f"Anime {anime_id} – Episode {episode}"

    # Compose final entry in required key order
    entry = {
        "url":   watch_url,
        "title": title,
    }
    entry.update(data)

    n = entry["found_streams"]
    print(f"  ✓ {n} stream(s) found")
    for i in range(1, n + 1):
        print(f"    stream_url_{i}: {entry.get(f'stream_url_{i}','')}")

    return entry


def main():
    if os.path.isfile(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                streams = json.load(f)
        except Exception:
            streams = {}
    else:
        streams = {}

    ok = 0
    for url in URLS:
        data = extract_one(url)
        if data:
            streams[url] = data
            ok += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(streams, f, indent=2, ensure_ascii=False)

    print(f"\nDone: {ok}/{len(URLS)} succeeded → {OUTPUT_FILE}")
    sys.exit(0 if ok == len(URLS) else 1)


if __name__ == "__main__":
    main()