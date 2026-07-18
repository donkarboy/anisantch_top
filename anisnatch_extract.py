"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.top
Extracts DUB streams only (skips SUB iframe).
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


def find_dub_iframe(page):
    """
    Looks for the DUB iframe specifically.
    AniSnatch typically has two iframes: one for SUB (/video/def/...) and one for DUB.
    We detect DUB by:
      1) A tab/button labelled 'DUB' that switches the iframe src, OR
      2) An iframe whose src contains 'dub' in the path, OR
      3) Clicking the DUB tab and reading the updated iframe src.
    Returns (frame, iframe_src) or (None, "").
    """
    iframe_src = ""
    frame = None

    # Try clicking the DUB tab/button if present
    try:
        dub_btn = page.query_selector('text=DUB') or \
                  page.query_selector('[data-type="dub"]') or \
                  page.query_selector('.dub-btn') or \
                  page.query_selector('button:has-text("DUB")') or \
                  page.query_selector('a:has-text("DUB")')
        if dub_btn:
            print("  [DUB] Found DUB tab — clicking it...")
            dub_btn.click()
            time.sleep(2)  # wait for iframe src to update
    except Exception as e:
        print(f"  [DUB] No DUB tab click: {e}")

    # Now find the iframe — prefer one whose src hints at 'dub'
    try:
        all_iframes = page.query_selector_all('iframe')
        for el in all_iframes:
            src = el.get_attribute("src") or ""
            if "dub" in src.lower():
                iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
                frame = el.content_frame()
                print(f"  [DUB] Matched dub iframe by src: {iframe_src}")
                break

        # Fallback: use the standard /video/def/ iframe (same one for dub after tab click)
        if not frame:
            el = page.query_selector('iframe[src*="/video/def/"]')
            if el:
                src = el.get_attribute("src") or ""
                iframe_src = src if src.startswith("http") else "https://anisnatch.top" + src
                frame = el.content_frame()
                print(f"  [DUB] Using default iframe (post-DUB-click): {iframe_src}")
    except Exception as e:
        print(f"  [DUB] iframe search error: {e}")

    return frame, iframe_src


def extract_one(watch_url, serial):
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r'/watch/(\d+)', watch_url) or [None, "?"])[1]
    episode  = (re.search(r'ep=(\d+)',     watch_url) or [None, "?"])[1]
    print(f"\n→ [{serial}] Anime {anime_id}  Ep {episode}  |  {watch_url}")

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

        # ── DUB-only: find and click DUB tab, then grab iframe ──
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

        html = frame.content()
        page_title = page.title()
        browser.close()

    data = extract_stream_data(html, iframe_src=iframe_src)
    if not any(k.startswith("stream_url_") for k in data):
        print("  [ERROR] No stream URL found in DUB iframe")
        return None

    title = page_title.strip() if page_title and page_title.strip() else f"Anime {anime_id} – Episode {episode}"

    # Final entry key order: serial → title → url → iframe_url → streams → mal_id → skips
    entry = {
        "serial": serial,
        "title":  title,
        "url":    watch_url,
    }
    entry.update(data)

    n = sum(1 for k in entry if k.startswith("stream_url_"))
    print(f"  ✓ serial={serial}  {n} DUB stream(s) found")
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

    # Determine next serial number based on existing entries
    existing_serials = [v.get("serial", 0) for v in streams.values() if isinstance(v, dict)]
    next_serial = max(existing_serials, default=0) + 1

    ok = 0
    for url in URLS:
        # Assign serial: reuse existing serial if URL already in file, else increment
        if url in streams and "serial" in streams[url]:
            serial = streams[url]["serial"]
        else:
            serial = next_serial
            next_serial += 1

        data = extract_one(url, serial)
        if data:
            streams[url] = data
            ok += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(streams, f, indent=2, ensure_ascii=False)

    print(f"\nDone: {ok}/{len(URLS)} succeeded → {OUTPUT_FILE}")
    sys.exit(0 if ok == len(URLS) else 1)


if __name__ == "__main__":
    main()
