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


def extract_stream_data(html, iframe_src=""):
    """
    Returns a dict with:
      iframe_url, stream_url_1..N, mal_id ("animeID/episode"), skips, subtitles
    Excluded: found_streams, episode, site_name, thumbnails
    """
    stream_urls = []

    # 1) const source = {src: {...}} block
    src_match = re.search(r'const\s+source\s*=\s*\{src\s*:\s*(\{[^}]+\})', html)
    if src_match:
        try:
            src_data = json.loads(src_match.group(1))
            u = src_data.get("url", "")
            if u and u not in stream_urls:
                stream_urls.append(u)
        except json.JSONDecodeError:
            pass

    # 2) All .m3u8 URLs in HTML
    for u in re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
        if u not in stream_urls:
            stream_urls.append(u)

    # 3) Expand multi-quality master URL into per-quality variants
    #    e.g. master contains ,1080p,720p,480p, → also add individual quality URLs
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

    # iframe URL
    if iframe_src:
        result["iframe_url"] = iframe_src

    # stream_url_1, stream_url_2, ...
    for i, u in enumerate(stream_urls, start=1):
        result[f"stream_url_{i}"] = u

    # mal_id as "animeID/episode"
    anime_id_m = re.search(r"animeID\s*=\s*['\"](\d+)['\"]", html)
    episode_m  = re.search(r"episodeNO\s*=\s*['\"](\d+)['\"]", html)
    if anime_id_m and episode_m:
        result["mal_id"] = f"{anime_id_m.group(1)}/{episode_m.group(1)}"
    elif anime_id_m:
        result["mal_id"] = anime_id_m.group(1)

    # skips
    m = re.search(r'skips\s*:\s*(\[.*?\])', html, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(1))
            if val:
                result["skips"] = val
        except json.JSONDecodeError:
            pass

    # subtitles (kept if present)
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
                raw_src = el.get_attribute("src") or ""
                # Build full iframe URL
                if raw_src.startswith("http"):
                    iframe_src = raw_src
                else:
                    iframe_src = "https://anisnatch.top" + raw_src
                frame = el.content_frame()
        except Exception:
            pass

        if not frame:
            print("  [ERROR] iframe not found")
            browser.close()
            return None

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            pass

        html = frame.content()
        page_title = page.title()
        browser.close()

    data = extract_stream_data(html, iframe_src=iframe_src)
    if not any(k.startswith("stream_url_") for k in data):
        print("  [ERROR] No stream URL found")
        return None

    title = page_title.strip() if page_title and page_title.strip() else f"Anime {anime_id} – Episode {episode}"

    # Final entry: title → url → iframe_url → stream_urls → mal_id → skips/subtitles
    entry = {
        "title": title,
        "url":   watch_url,
    }
    entry.update(data)

    n = sum(1 for k in entry if k.startswith("stream_url_"))
    print(f"  ✓ {n} stream(s) found")
    for i in range(1, n + 1):
        print(f"    stream_url_{i}: {entry.get(f'stream_url_{i}', '')}")

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
