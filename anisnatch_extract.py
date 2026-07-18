"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.to
─────────────────────────────────────────────────────────────
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
- Extracts DUB streams only.
- Batch size controlled by CLI arg: python anisnatch_extract.py --limit 100

IFRAME URL DECODING (all 9 server types handled without any iframe navigation):
─────────────────────────────────────────────────────────────────────────────────

  1. AllAnime   | def/           | hex + XOR 0x06 → JSON  → Wix CDN URLs (mp4 + m3u8)
  2. AniVibe    | vibeplayer/    | base64            → https://vivibebe.site/{hash}
  3. AniYT      | yt-mp4/        | plain slug        → token extracted
  4. Megaplay   | megaplay/      | plain {id}-dub    → numeric ID extracted
  5. Vidwish    | vidwish/       | plain {id}-dub    → numeric ID extracted
  6. OkCdn      | ok/            | plain numeric     → ok.ru embed URL built
  7. MP4        | mp4/           | plain slug        → slug extracted
  8. Swift      | swift/         | plain token       → token extracted
  9. AniCdn     | anicdn/        | plain md5 hash    → hash extracted

AllAnime decoding detail:
  The token in def/{token}/{animeId}-{ep} is a hex string.
  Each byte XOR'd with 0x06 yields JSON:
    {
      "url":             "<mediaId> | <secondaryId> | media/<thumb> | ,480p,720p,",
      "streamerId":      "Wix",
      "date":            "2026-...",
      "translationType": "dub",
      "key":             "ep-{animeKey}_{ep}_dub"
    }
  From the mediaId we build:
    https://static.wixstatic.com/videos/{mediaId}/mp4/{mediaId},{quality}.mp4
    https://static.wixstatic.com/videos/{mediaId}/file/{mediaId}.m3u8
─────────────────────────────────────────────────────────────────────────────────
"""

import re
import json
import os
import sys
import time
import glob
import base64
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
IFRAME_BASE    = "https://anisnatch.to/video/"
# ─────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════
# SECTION 1 — IFRAME URL DECODERS
# ══════════════════════════════════════════════════════════════════

def _decode_allanime_def(hex_token: str) -> dict:
    """
    AllAnime / def/ server decoder.

    The hex_token is a raw hex string.  Each byte XOR'd with 0x06 produces
    a JSON object:
        {
          "url":             "<mediaId> | <secId> | media/<thumb> | ,480p,720p,",
          "streamerId":      "Wix",
          "date":            "...",
          "translationType": "dub",
          "key":             "ep-{animeKey}_{ep}_dub"
        }

    Returns a dict with keys:
        raw_json    – the full decoded JSON object
        media_id    – Wix media identifier
        secondary_id
        thumbnail   – relative Wix media thumbnail path
        qualities   – list of quality strings, e.g. ["480p", "720p"]
        stream_urls – list of direct MP4 URLs per quality
        m3u8_url    – HLS master playlist URL
        streamer_id – "Wix"
        key         – "ep-vDTSJHSpYnrkZnAvG_250_dub"
        date        – ISO date string from JSON
    """
    result = {
        "raw_json":     {},
        "media_id":     "",
        "secondary_id": "",
        "thumbnail":    "",
        "qualities":    [],
        "stream_urls":  [],
        "m3u8_url":     "",
        "streamer_id":  "",
        "key":          "",
        "date":         "",
    }
    try:
        raw_bytes = bytes.fromhex(hex_token)
    except ValueError as e:
        result["error"] = f"hex decode failed: {e}"
        return result

    xored = bytes([b ^ 0x06 for b in raw_bytes])
    try:
        data = json.loads(xored.decode("latin-1"))
    except Exception as e:
        result["error"] = f"JSON parse failed: {e}"
        return result

    result["raw_json"]    = data
    result["streamer_id"] = data.get("streamerId", "")
    result["key"]         = data.get("key", "")
    result["date"]        = data.get("date", "")

    url_field = data.get("url", "")
    parts     = [p.strip() for p in url_field.split(" | ")]

    if len(parts) >= 1:
        result["media_id"]     = parts[0]
    if len(parts) >= 2:
        result["secondary_id"] = parts[1]
    if len(parts) >= 3:
        result["thumbnail"]    = parts[2]   # e.g. "media/abc_xyz.jpg"
    if len(parts) >= 4:
        # ",480p,720p," → ["480p", "720p"]
        result["qualities"] = [q.strip() for q in parts[3].split(",") if q.strip()]

    media_id = result["media_id"]
    if media_id:
        for q in result["qualities"]:
            result["stream_urls"].append(
                f"https://static.wixstatic.com/videos/{media_id}/mp4/{media_id},{q}.mp4"
            )
        result["m3u8_url"] = (
            f"https://static.wixstatic.com/videos/{media_id}/file/{media_id}.m3u8"
        )

    return result


def _decode_vibeplayer_b64(b64_token: str) -> str:
    """
    AniVibe / vibeplayer/ server decoder.
    The token is a base64-encoded URL (standard base64, may lack padding).
    Returns the decoded URL string, e.g. "https://vivibebe.site/20502e92cd859a00".
    """
    # Add padding if needed
    padded = b64_token + "=" * (-len(b64_token) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8")
    except Exception as e:
        return f"ERROR: {e}"


def decode_iframe_url(iframe_url: str) -> dict:
    """
    Master decoder for any https://anisnatch.to/video/* iframe URL.

    Returns a unified dict:
        server_type   – "def" | "vibeplayer" | "yt-mp4" | "megaplay" | ...
        token         – the raw token extracted from the URL path
        ep_suffix     – "{animeId}-{epNo}", e.g. "1735-250"
        iframe_url    – original iframe URL (always present)
        stream_urls   – list of playable URLs (may be player pages for some servers)
        m3u8_url      – HLS URL (AllAnime only, else "")
        qualities     – list of quality strings (AllAnime only, else [])
        extra         – server-specific extra data dict

    Notes per server_type:
        def         → stream_urls are direct Wix CDN MP4s; m3u8_url is the HLS master
        vibeplayer  → stream_urls[0] is the vivibebe.site player page URL
        yt-mp4      → stream_urls[0] is the original iframe_url (needs iframe for MP4)
        megaplay    → extra["megaplay_id"] = numeric string; stream_urls[0] = iframe_url
        vidwish     → extra["vidwish_id"]  = numeric string; stream_urls[0] = iframe_url
        ok          → extra["ok_video_id"] = numeric string; stream_urls[0] = ok.ru embed URL
        mp4         → extra["mp4_slug"]    = slug string;    stream_urls[0] = iframe_url
        swift       → extra["swift_token"] = token string;   stream_urls[0] = iframe_url
        anicdn      → extra["anicdn_hash"] = md5 string;     stream_urls[0] = iframe_url
    """
    result = {
        "server_type": "",
        "token":       "",
        "ep_suffix":   "",
        "iframe_url":  iframe_url,
        "stream_urls": [],
        "m3u8_url":    "",
        "qualities":   [],
        "extra":       {},
    }

    if not iframe_url.startswith(IFRAME_BASE):
        result["error"] = "not an anisnatch /video/ URL"
        return result

    path = iframe_url[len(IFRAME_BASE):]  # strip the base prefix

    # Episode suffix is always the last path segment: /{animeId}-{epNo}
    ep_m = re.search(r"/(\d+-\d+)$", path)
    if ep_m:
        result["ep_suffix"] = ep_m.group(1)

    # First path segment = server type identifier
    first_slash = path.find("/")
    if first_slash == -1:
        result["error"] = "unexpected URL structure (no slash after server type)"
        return result

    server_type = path[:first_slash]
    rest        = path[first_slash + 1:]           # everything after server_type/

    # Remove episode suffix from rest to isolate the token
    ep_sfx = result["ep_suffix"]
    if ep_sfx and rest.endswith("/" + ep_sfx):
        token = rest[: -(len(ep_sfx) + 1)]
    else:
        token = rest

    result["server_type"] = server_type
    result["token"]       = token

    # ── Per-server decoding ──────────────────────────────────────

    if server_type == "def":
        # AllAnime: token is raw hex, XOR 0x06 → JSON → Wix CDN
        dec = _decode_allanime_def(token)
        result["stream_urls"] = dec.get("stream_urls", [])
        result["m3u8_url"]    = dec.get("m3u8_url", "")
        result["qualities"]   = dec.get("qualities", [])
        result["extra"]       = {
            "media_id":     dec.get("media_id", ""),
            "secondary_id": dec.get("secondary_id", ""),
            "thumbnail":    dec.get("thumbnail", ""),
            "streamer_id":  dec.get("streamer_id", ""),
            "wix_key":      dec.get("key", ""),
            "date":         dec.get("date", ""),
            "raw_json":     dec.get("raw_json", {}),
        }
        if "error" in dec:
            result["error"] = dec["error"]

    elif server_type == "vibeplayer":
        # AniVibe: token is base64-encoded player URL
        player_url = _decode_vibeplayer_b64(token)
        result["stream_urls"] = [player_url]
        result["extra"]       = {"player_url": player_url}

    elif server_type == "yt-mp4":
        # AniYT: token is "{animeKey}-{ep}-dub"
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"yt_key": token}

    elif server_type == "megaplay":
        # Megaplay: token is "{numericId}-dub"
        megaplay_id = token.removesuffix("-dub")
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"megaplay_id": megaplay_id}

    elif server_type == "vidwish":
        # Vidwish: token is "{numericId}-dub"
        vidwish_id = token.removesuffix("-dub")
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"vidwish_id": vidwish_id}

    elif server_type == "ok":
        # OkCdn: token is the ok.ru video ID (numeric string)
        ok_embed = f"https://ok.ru/videoembed/{token}"
        result["stream_urls"] = [ok_embed]
        result["extra"]       = {
            "ok_video_id": token,
            "ok_embed_url": ok_embed,
        }

    elif server_type == "mp4":
        # MP4: token is a CDN slug for a direct mp4
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"mp4_slug": token}

    elif server_type == "swift":
        # Swift CDN: token is an access token
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"swift_token": token}

    elif server_type == "anicdn":
        # AniCDN: token is an MD5 hash
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"anicdn_hash": token}

    else:
        # Unknown server type — keep iframe_url as fallback
        result["stream_urls"] = [iframe_url]
        result["extra"]       = {"unknown_token": token}

    return result


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — SPLIT-FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def all_output_files():
    base     = glob.glob(OUTPUT_BASE + OUTPUT_EXT)
    numbered = sorted(
        glob.glob(f"{OUTPUT_BASE}_*{OUTPUT_EXT}"),
        key=lambda f: int(re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f).group(1))
        if re.search(r'_(\d+)' + re.escape(OUTPUT_EXT) + r'$', f) else 0,
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


def save_entry_to_file(url: str, entry: dict) -> str:
    target = current_write_target()
    bucket: dict = {}
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


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — PROCESSED / ERROR LOGS
# ══════════════════════════════════════════════════════════════════

def load_processed_urls() -> set:
    if not os.path.isfile(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_processed(url: str):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def mark_error(url: str, reason: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}]  {url}  |  {reason}\n")


# ══════════════════════════════════════════════════════════════════
# SECTION 4 — INPUT URL LIST
# ══════════════════════════════════════════════════════════════════

def load_input_urls() -> list:
    if not os.path.isfile(INPUT_FILE):
        print(f"[ERROR] Input file not found: {INPUT_FILE}")
        sys.exit(1)
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    print(f"[INFO] {len(urls)} URL(s) in {INPUT_FILE}")
    return urls


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — PLAYWRIGHT HELPERS (DUB SELECTION + DOM EXTRACTION)
# ══════════════════════════════════════════════════════════════════

def ensure_dub_selected(page) -> bool:
    """
    Ensure the DUB server type is active.
    Returns True if DUB is (now) active, False if no DUB exists for this episode.
    """
    try:
        page.wait_for_selector("#server-option", timeout=15_000)
    except Exception:
        print("  [DUB] #server-option not found — page load issue")
        return False

    server_type_btn = page.query_selector("#serverType")
    if server_type_btn:
        current_val = server_type_btn.get_attribute("data-value") or ""
        if current_val.lower() == "dub":
            print("  [DUB] DUB already active (data-value=dub on #serverType)")
            return True

    # Disable partPlayer overlay so it doesn't intercept clicks
    try:
        overlay = page.query_selector("div.partPlayer")
        if overlay:
            page.evaluate(
                "() => { const el = document.querySelector('div.partPlayer');"
                "if (el) el.style.pointerEvents = 'none'; }"
            )
            print("  [DUB] Disabled partPlayer overlay via JS")
    except Exception:
        pass

    # Open the serverTypeMenu dropdown
    try:
        page.evaluate(
            "() => { const btn = document.querySelector('#serverType');"
            "if (btn) btn.click(); }"
        )
        time.sleep(0.5)
    except Exception:
        pass

    dub_item = page.query_selector('#serverTypeMenu .dropdown-item[data-type="dub"]')
    if not dub_item:
        print("  [DUB] No [data-type='dub'] item in #serverTypeMenu — no dub for this episode")
        return False

    try:
        print("  [DUB] Clicking [data-type='dub'] in #serverTypeMenu …")
        clicked = page.evaluate("""
            () => {
                const item = document.querySelector(
                    '#serverTypeMenu .dropdown-item[data-type="dub"]');
                if (!item) return false;
                item.dispatchEvent(
                    new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            }
        """)
        if not clicked:
            print("  [DUB] JS click returned false — element gone after query")
            return False
        time.sleep(2.5)
    except Exception as e:
        try:
            print(f"  [DUB] JS click failed ({e}), trying force click …")
            dub_item.click(force=True)
            time.sleep(2.5)
        except Exception as e2:
            print(f"  [DUB] Click failed: {e2}")
            return False

    # Confirm DUB is now active
    server_type_btn = page.query_selector("#serverType")
    if server_type_btn:
        val = (server_type_btn.get_attribute("data-value") or "").lower()
        if val == "dub":
            print("  [DUB] DUB confirmed active after click")
            return True

    active_dub = page.query_selector(
        '#serverTypeMenu .dropdown-item.active[data-type="dub"]'
    )
    if active_dub:
        print("  [DUB] DUB confirmed active via .active class")
        return True

    print("  [DUB] Could not confirm DUB selection after click")
    return False


def extract_servers_from_dom(page) -> list:
    """
    Read all server entries from #streamTypeMenu after DUB is selected.

    Returns list of dicts:
        server      – data-server attribute
        source      – data-source attribute (the token path after /video/)
        label       – human-readable server label
        info        – optional badge text (e.g. "MP4", "HINDI", "MULTI")
        active      – bool, True if this server is currently selected
        iframe_url  – https://anisnatch.to/video/ + source
    """
    servers = []
    try:
        items = page.query_selector_all("#streamTypeMenu .dropdown-item")
        for item in items:
            source = item.get_attribute("data-source") or ""
            if not source:
                continue

            server = item.get_attribute("data-server") or ""

            label_el = item.query_selector(".item-text.text-title, .item-text")
            label    = label_el.inner_text().strip() if label_el else server

            info_el  = item.query_selector(".item-info")
            info     = info_el.inner_text().strip() if info_el else ""

            is_active  = "active" in (item.get_attribute("class") or "")
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


def extract_meta_from_iframe(page) -> dict:
    """Extract skips, subtitles, animeID, episodeNO from the active iframe HTML."""
    result = {}
    try:
        iframe_el = page.query_selector("iframe#video-player")
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

        m = re.search(r"skips\s*:\s*(\[.*?\])", html, re.DOTALL)
        if m:
            try:
                val = json.loads(m.group(1))
                if val:
                    result["skips"] = val
            except json.JSONDecodeError:
                pass

        m = re.search(r"subtitles\s*:\s*(\[.*?\])", html, re.DOTALL)
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


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — SINGLE URL PROCESSOR
# ══════════════════════════════════════════════════════════════════

def extract_one(watch_url: str, serial: int) -> dict | None:
    from playwright.sync_api import sync_playwright

    anime_id = (re.search(r"/watch/(\d+)", watch_url) or [None, "?"])[1]
    episode  = (re.search(r"ep=(\d+)",     watch_url) or [None, "?"])[1]
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
            time.sleep(2)
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

        # ── 3. Read ALL server entries from DOM ────────────────────
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

        # ── 4. Try to pull meta from the active iframe ─────────────
        meta = extract_meta_from_iframe(page)

        page_title = page.title()
        browser.close()

    # ── 5. Decode every iframe URL (no further browser calls needed) ──
    decoded_servers = []
    all_stream_urls = []   # flat list across all servers, deduped
    seen_urls       = set()

    for s in servers:
        decoded = decode_iframe_url(s["iframe_url"])
        server_entry = {
            "server":      s["server"],
            "label":       s["label"],
            "info":        s["info"],
            "active":      s["active"],
            "server_type": decoded["server_type"],
            "iframe_url":  s["iframe_url"],
            "token":       decoded["token"],
            "ep_suffix":   decoded["ep_suffix"],
            "stream_urls": decoded["stream_urls"],
            "m3u8_url":    decoded.get("m3u8_url", ""),
            "qualities":   decoded.get("qualities", []),
            "extra":       decoded.get("extra", {}),
        }
        if "error" in decoded:
            server_entry["decode_error"] = decoded["error"]

        decoded_servers.append(server_entry)

        # Collect unique stream URLs across all servers
        for u in decoded["stream_urls"]:
            if u and u not in seen_urls:
                seen_urls.add(u)
                all_stream_urls.append(u)

        # Print decoded results
        is_active = " ← active" if s["active"] else ""
        stype = decoded["server_type"]
        print(f"  [DECODE] {s['label']}{is_active}  type={stype}")

        if stype == "def":
            extra = decoded.get("extra", {})
            print(f"    media_id:   {extra.get('media_id', '')}")
            print(f"    qualities:  {decoded.get('qualities', [])}")
            if decoded.get("m3u8_url"):
                print(f"    m3u8:       {decoded['m3u8_url']}")
            for u in decoded["stream_urls"]:
                print(f"    mp4:        {u}")
        elif stype == "vibeplayer":
            print(f"    player_url: {decoded.get('extra', {}).get('player_url', '')}")
        elif stype == "ok":
            print(f"    ok_embed:   {decoded.get('extra', {}).get('ok_embed_url', '')}")
        else:
            for u in decoded["stream_urls"]:
                print(f"    stream:     {u}")

    # ── 6. Build output entry ──────────────────────────────────────
    title = (
        page_title.strip()
        if page_title and page_title.strip()
        else f"Anime {anime_id} – Episode {episode}"
    )

    entry = {
        "serial":       serial,
        "title":        title,
        "url":          watch_url,
        "type":         "dub",
        "dub_servers":  decoded_servers,
    }

    # Top-level stream_url_N from all decoded URLs (deduped, ordered)
    for i, u in enumerate(all_stream_urls, start=1):
        entry[f"stream_url_{i}"] = u

    # Fallback: if nothing decoded at all, use first iframe URL
    if not all_stream_urls and servers:
        fallback = servers[0]["iframe_url"]
        entry["stream_url_1"] = fallback
        print(f"  [WARN] No decoded stream URLs — using first iframe as stream_url_1")

    entry.update(meta)

    n_servers = len(servers)
    n_streams = sum(1 for k in entry if k.startswith("stream_url_"))
    print(f"  ✓ serial={serial}  {n_servers} DUB server(s)  {n_streams} decoded stream(s)")
    for i in range(1, n_streams + 1):
        print(f"    stream_url_{i}: {entry.get(f'stream_url_{i}', '')}")

    return entry


# ══════════════════════════════════════════════════════════════════
# SECTION 7 — CLI & MAIN
# ══════════════════════════════════════════════════════════════════

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


def resolve_limit(raw: str) -> int | None:
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
