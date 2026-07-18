"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.to
─────────────────────────────────────────────────────────────
- Reads input URLs from:      inputed_urls_list.txt
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
- Writes output to:           streams.json, streams_2.json … (auto-splits at 3 MB)
- Extracts DUB streams only.
- Batch size controlled by CLI arg: python anisnatch_extract.py --limit 100

OUTPUT JSON FORMAT (one entry per watch-URL, flat keys):
─────────────────────────────────────────────────────────
  {
    "serial":                              1,
    "title":                               "Naruto: Shippuuden - AniSnatch",
    "url":                                 "https://anisnatch.top/watch/1735?ep=250",
    "mal_id_with_ep_and_stream_type":      "1735/250==dub",

    // AllAnime / def  (always separate iframe + per-quality m3u8 keys)
    "allanime_iframe":   "<iframe url>",
    "allanime_480":      "<480p m3u8>",
    "allanime_720":      "<720p m3u8>",
    "allanime_1080":     "<1080p m3u8>",

    // AniVibe / vibeplayer  (iframe + decoded player URL)
    "anivibe_iframe":    "<iframe url>",
    "anivibe":           "https://vivibebe.site/<hash>",

    // AniYT / yt-mp4  (iframe == stream, one combined key)
    "aniyt_and_aniyt_iframe_are_same_url":       "<iframe url>",

    // Megaplay / megaplay  (iframe == stream, one combined key)
    "megaplay_and_megaplay_iframe_are_same_url": "<iframe url>",

    // Vidwish / vidwish  (iframe == stream, one combined key)
    "vidwish_and_vidwish_iframe_are_same_url":   "<iframe url>",

    // OkCdn / ok  (iframe + decoded ok.ru embed URL)
    "okcdn_iframe":      "<iframe url>",
    "okcdn":             "https://ok.ru/videoembed/<id>",

    // MP4 / mp4  (iframe == stream, one combined key)
    "mp4_and_mp4_iframe_are_same_url":           "<iframe url>",

    // Swift / swift  (iframe == stream, one combined key)
    "swift_and_swift_iframe_are_same_url":       "<iframe url>",

    // AniCdn / anicdn  (iframe == stream, one combined key)
    "anicdn_and_anicdn_iframe_are_same_url":     "<iframe url>",

    // Optional: skip_marks / subtitles if found in page/iframe/JS
    "skip_marks": [{"label": "Intro", "start": 0, "end": 90}, ...],
    "subtitles":  [...],
  }

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
  From the mediaId we build per-quality repackager m3u8s:
    https://repackager.wixmp.com/video.wixstatic.com/video/{mediaId}/,480p/mp4/file.mp4.urlset/master.m3u8
    https://repackager.wixmp.com/video.wixstatic.com/video/{mediaId}/,720p/mp4/file.mp4.urlset/master.m3u8
    https://repackager.wixmp.com/video.wixstatic.com/video/{mediaId}/,1080p/mp4/file.mp4.urlset/master.m3u8
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
        m3u8_480    – 480p per-quality repackager HLS URL  (always built)
        m3u8_720    – 720p per-quality repackager HLS URL  (always built)
        m3u8_1080   – 1080p per-quality repackager HLS URL (always built)
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
        "m3u8_480":     "",
        "m3u8_720":     "",
        "m3u8_1080":    "",
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
        base = (
            f"https://repackager.wixmp.com/video.wixstatic.com/video"
            f"/{media_id}"
        )
        result["m3u8_480"]  = f"{base}/,480p/mp4/file.mp4.urlset/master.m3u8"
        result["m3u8_720"]  = f"{base}/,720p/mp4/file.mp4.urlset/master.m3u8"
        result["m3u8_1080"] = f"{base}/,1080p/mp4/file.mp4.urlset/master.m3u8"

    return result


def _decode_vibeplayer_b64(b64_token: str) -> str:
    """
    AniVibe / vibeplayer/ server decoder.
    The token is a base64-encoded URL (standard base64, may lack padding).
    Returns the decoded URL string, e.g. "https://vivibebe.site/20502e92cd859a00".
    """
    padded = b64_token + "=" * (-len(b64_token) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8")
    except Exception as e:
        return f"ERROR: {e}"


def decode_iframe_url(iframe_url: str) -> dict:
    """
    Master decoder for any https://anisnatch.to/video/* iframe URL.

    Returns a unified dict:
        server_type  – "def" | "vibeplayer" | "yt-mp4" | "megaplay" | ...
        token        – the raw token extracted from the URL path
        ep_suffix    – "{animeId}-{epNo}", e.g. "1735-250"
        iframe_url   – original iframe URL (always present)
        extra        – server-specific decoded data
    """
    result = {
        "server_type": "",
        "token":       "",
        "ep_suffix":   "",
        "iframe_url":  iframe_url,
        "extra":       {},
    }

    if not iframe_url.startswith(IFRAME_BASE):
        result["error"] = "not an anisnatch /video/ URL"
        return result

    path = iframe_url[len(IFRAME_BASE):]

    ep_m = re.search(r"/(\d+-\d+)$", path)
    if ep_m:
        result["ep_suffix"] = ep_m.group(1)

    first_slash = path.find("/")
    if first_slash == -1:
        result["error"] = "unexpected URL structure (no slash after server type)"
        return result

    server_type = path[:first_slash]
    rest        = path[first_slash + 1:]

    ep_sfx = result["ep_suffix"]
    if ep_sfx and rest.endswith("/" + ep_sfx):
        token = rest[: -(len(ep_sfx) + 1)]
    else:
        token = rest

    result["server_type"] = server_type
    result["token"]       = token

    # ── Per-server decoding ──────────────────────────────────────

    if server_type == "def":
        dec = _decode_allanime_def(token)
        result["extra"] = dec
        if "error" in dec:
            result["error"] = dec["error"]

    elif server_type == "vibeplayer":
        player_url = _decode_vibeplayer_b64(token)
        result["extra"] = {"player_url": player_url}

    elif server_type == "yt-mp4":
        result["extra"] = {"yt_key": token}

    elif server_type == "megaplay":
        result["extra"] = {"megaplay_id": token.removesuffix("-dub")}

    elif server_type == "vidwish":
        result["extra"] = {"vidwish_id": token.removesuffix("-dub")}

    elif server_type == "ok":
        result["extra"] = {
            "ok_video_id":  token,
            "ok_embed_url": f"https://ok.ru/videoembed/{token}",
        }

    elif server_type == "mp4":
        result["extra"] = {"mp4_slug": token}

    elif server_type == "swift":
        result["extra"] = {"swift_token": token}

    elif server_type == "anicdn":
        result["extra"] = {"anicdn_hash": token}

    else:
        result["extra"] = {"unknown_token": token}

    return result


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — FLAT ENTRY BUILDER
# ══════════════════════════════════════════════════════════════════

# Maps server_type → the key name used when iframe == stream (combined key)
_SAME_KEY = {
    "yt-mp4":   "aniyt_and_aniyt_iframe_are_same_url",
    "megaplay": "megaplay_and_megaplay_iframe_are_same_url",
    "vidwish":  "vidwish_and_vidwish_iframe_are_same_url",
    "mp4":      "mp4_and_mp4_iframe_are_same_url",
    "swift":    "swift_and_swift_iframe_are_same_url",
    "anicdn":   "anicdn_and_anicdn_iframe_are_same_url",
}

# Canonical server-type order for key insertion
_SERVER_ORDER = [
    "def",        # AllAnime
    "vibeplayer", # AniVibe
    "yt-mp4",     # AniYT
    "megaplay",   # Megaplay
    "vidwish",    # Vidwish
    "ok",         # OkCdn
    "mp4",        # MP4
    "swift",      # Swift
    "anicdn",     # AniCdn
]


def build_flat_entry(
    serial:     int,
    title:      str,
    watch_url:  str,
    anime_id:   str,
    episode:    str,
    servers:    list,   # from extract_servers_from_dom()
    meta:       dict,   # from extract_meta_from_iframe()
) -> dict:
    """
    Build the flat output dict that matches the required JSON format exactly.

    Fixed top-level keys (always present):
        serial, title, url, mal_id_with_ep_and_stream_type

    Per-server keys (only added when that server is present on the page):
        AllAnime  → allanime_iframe, allanime_480, allanime_720, allanime_1080
        AniVibe   → anivibe_iframe, anivibe
        AniYT     → aniyt_and_aniyt_iframe_are_same_url
        Megaplay  → megaplay_and_megaplay_iframe_are_same_url
        Vidwish   → vidwish_and_vidwish_iframe_are_same_url
        OkCdn     → okcdn_iframe, okcdn
        MP4       → mp4_and_mp4_iframe_are_same_url
        Swift     → swift_and_swift_iframe_are_same_url
        AniCdn    → anicdn_and_anicdn_iframe_are_same_url

    Optional meta keys (only when found in page/iframe HTML or JS globals):
        skip_marks, subtitles
    """

    # ── 1. Decode every server's iframe URL ──────────────────────
    decoded_map: dict[str, list] = {}   # server_type → list of decoded results
    for s in servers:
        dec = decode_iframe_url(s["iframe_url"])
        stype = dec["server_type"] or "unknown"
        decoded_map.setdefault(stype, []).append(dec)

        # Console output
        is_active = " ← active" if s["active"] else ""
        print(f"  [DECODE] {s['label']}{is_active}  type={stype}")

        if stype == "def":
            extra = dec.get("extra", {})
            print(f"    media_id:   {extra.get('media_id', '')}")
            print(f"    qualities:  {extra.get('qualities', [])}")
            print(f"    allanime_480:  {extra.get('m3u8_480', '')}")
            print(f"    allanime_720:  {extra.get('m3u8_720', '')}")
            print(f"    allanime_1080: {extra.get('m3u8_1080', '')}")
        elif stype == "vibeplayer":
            print(f"    player_url: {dec['extra'].get('player_url', '')}")
        elif stype == "ok":
            print(f"    ok_embed:   {dec['extra'].get('ok_embed_url', '')}")
        else:
            print(f"    iframe_url: {dec['iframe_url']}")

    # ── 2. Build the flat entry in canonical key order ────────────
    entry: dict = {}

    entry["serial"] = serial
    entry["title"]  = title
    entry["url"]    = watch_url
    entry["mal_id_with_ep_and_stream_type"] = f"{anime_id}/{episode}==dub"

    # Iterate servers in canonical order so JSON keys appear in a consistent,
    # human-readable sequence regardless of DOM order.
    for stype in _SERVER_ORDER:
        decs = decoded_map.get(stype, [])
        if not decs:
            continue

        # Take the first occurrence (in practice there's only ever one per type)
        dec   = decs[0]
        extra = dec.get("extra", {})
        iurl  = dec["iframe_url"]

        if stype == "def":
            entry["allanime_iframe"] = iurl
            entry["allanime_480"]    = extra.get("m3u8_480",  "")
            entry["allanime_720"]    = extra.get("m3u8_720",  "")
            entry["allanime_1080"]   = extra.get("m3u8_1080", "")

        elif stype == "vibeplayer":
            entry["anivibe_iframe"] = iurl
            entry["anivibe"]        = extra.get("player_url", "")

        elif stype in _SAME_KEY:
            entry[_SAME_KEY[stype]] = iurl

        elif stype == "ok":
            entry["okcdn_iframe"] = iurl
            entry["okcdn"]        = extra.get("ok_embed_url", "")

        else:
            # Unknown / future server type: store iframe URL with a generic key
            entry[f"{stype}_iframe"] = iurl

    # ── 3. Optional meta (skip_marks / subtitles) ────────────────
    if "skip_marks" in meta:
        entry["skip_marks"] = meta["skip_marks"]
    if "subtitles" in meta:
        entry["subtitles"] = meta["subtitles"]

    return entry


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — SPLIT-FILE MANAGEMENT
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
# SECTION 4 — PROCESSED / ERROR LOGS
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
# SECTION 5 — INPUT URL LIST
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
# SECTION 6 — PLAYWRIGHT HELPERS (DUB SELECTION + DOM EXTRACTION)
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


def _extract_json_array(html: str, key: str):
    """
    Robustly extract the first JSON array assigned to `key` in an HTML/JS blob.

    Handles all common patterns:
        key: [...]                   (JS object literal)
        key = [...]                  (JS variable assignment)
        "key": [...]                 (JSON property)
        'key': [...]                 (single-quoted JS)
        var key=[...]
        window.key=[...]
        data-key='[...]'             (HTML attribute)

    Uses bracket counting to find the exact closing ] even across multiple lines,
    nested arrays/objects, and escaped quotes — far more reliable than .*? regex.
    """
    # Build a pattern that matches the start of the array for any of the forms above
    pattern = re.compile(
        r"""(?:
            (?:var\s+|window\.|let\s+|const\s+)?   # optional JS prefix
            (?:["']?)                               # optional opening quote
            """ + re.escape(key) + r"""
            (?:["']?)                               # optional closing quote
            \s* [=:] \s*                            # = or :
        |
            data-""" + re.escape(key) + r"""\s*=\s*['"]  # HTML data-attr
        )
        (\[)                                        # capture the opening [
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    for m in pattern.finditer(html):
        start = m.start(1)   # position of the opening [
        depth = 0
        in_str  = False
        str_ch  = ""
        i = start
        while i < len(html):
            ch = html[i]
            if in_str:
                if ch == "\\" :
                    i += 2          # skip escaped character
                    continue
                if ch == str_ch:
                    in_str = False
            else:
                if ch in ('"', "'"):
                    in_str = True
                    str_ch = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        raw = html[start : i + 1]
                        try:
                            val = json.loads(raw)
                            if isinstance(val, list) and val:
                                return val
                        except json.JSONDecodeError:
                            # Try single→double quote fix for JS literals
                            try:
                                val = json.loads(raw.replace("'", '"'))
                                if isinstance(val, list) and val:
                                    return val
                            except Exception:
                                pass
                        break   # malformed — try next match
            i += 1

    return None


def extract_meta_from_iframe(page) -> dict:
    """
    Extract skip_marks and subtitles from every available HTML source.

    Search order:
      1. Main page HTML
      2. Active iframe HTML  (after waiting for it to load)
      3. JS evaluation INSIDE the iframe frame  (most reliable — skips lives here)
      4. JS evaluation on the main page as a last-ditch attempt

    The result key is always "skip_marks" (never "skips").
    """
    result  = {}
    sources = []   # list of (label, html_string) to search

    # ── Source 1: main page HTML ──────────────────────────────────
    try:
        sources.append(("main-page", page.content()))
    except Exception as e:
        print(f"  [META] Could not read main page HTML: {e}")

    # ── Source 2 + 3: iframe HTML and JS eval INSIDE the frame ───
    # The player page (anisnatch.to/video/def/...) sets JS variables like
    #   skips = [{label:'Intro',start:0,end:90}, ...]
    # inside its own <script> block.  We must evaluate these expressions
    # in the iframe's JS context, not the parent page's context.
    try:
        # Wait up to 10 s for the iframe to appear
        page.wait_for_selector(
            "iframe#video-player, iframe[src*='/video/']",
            timeout=10_000,
        )
        iframe_el = (page.query_selector("iframe#video-player") or
                     page.query_selector("iframe[src*='/video/']"))
        if iframe_el:
            frame = iframe_el.content_frame()
            if frame:
                # Wait for the frame's DOM to be ready
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    time.sleep(2)

                # Source 2: raw iframe HTML
                try:
                    iframe_html = frame.content()
                    if iframe_html and len(iframe_html) > 200:
                        sources.append(("iframe-html", iframe_html))
                        print(f"  [META] Got iframe HTML ({len(iframe_html):,} bytes)")
                    else:
                        print("  [META] iframe HTML too small — skipping")
                except Exception as e:
                    print(f"  [META] Could not read iframe HTML: {e}")

                # Source 3: JS eval INSIDE the iframe frame
                # skips is a plain var in the player's <script>, not on window.*
                # of the parent — so we MUST evaluate inside the iframe frame.
                js_vars = [
                    "typeof skips      !== 'undefined' ? skips      : null",
                    "typeof skipTimes  !== 'undefined' ? skipTimes  : null",
                    "typeof skipMarks  !== 'undefined' ? skipMarks  : null",
                    "typeof introOutro !== 'undefined' ? introOutro : null",
                    "typeof chapters   !== 'undefined' ? chapters   : null",
                    "window.skips",
                    "window.skipTimes",
                    "window.skipMarks",
                    "window.playerConfig && window.playerConfig.skips",
                    "window.playerConfig && window.playerConfig.skipTimes",
                ]
                for expr in js_vars:
                    try:
                        val = frame.evaluate(
                            f"() => {{ try {{ return {expr}; }} catch(e) {{ return null; }} }}"
                        )
                        if isinstance(val, list) and val:
                            sources.append(("iframe-js", json.dumps({"skips": val})))
                            print(f"  [META] iframe-js found skips via ({expr}): {val}")
                            break
                    except Exception:
                        pass
    except Exception as e:
        print(f"  [META] iframe access failed: {e}")

    # ── Source 4: JS eval on main page (last resort) ─────────────
    for expr in [
        "typeof skips     !== 'undefined' ? skips     : null",
        "typeof skipTimes !== 'undefined' ? skipTimes : null",
        "typeof skipMarks !== 'undefined' ? skipMarks : null",
        "window.skips", "window.skipTimes", "window.skipMarks",
    ]:
        try:
            val = page.evaluate(
                f"() => {{ try {{ return {expr}; }} catch(e) {{ return null; }} }}"
            )
            if isinstance(val, list) and val:
                sources.append(("main-js", json.dumps({"skips": val})))
                print(f"  [META] main-js found skips via ({expr}): {val}")
                break
        except Exception:
            pass

    # ── Skip-mark key aliases to search (in priority order) ───────
    SKIP_KEYS     = ["skips", "skipTimes", "skipMarks", "skip_marks",
                     "introOutro", "intro_outro", "chapters", "cuePoints"]
    SUBTITLE_KEYS = ["subtitles", "tracks", "captions", "textTracks"]

    for label, html in sources:
        if not html:
            continue

        # Skip marks
        if "skip_marks" not in result:
            for key in SKIP_KEYS:
                val = _extract_json_array(html, key)
                if val:
                    # Normalise: each item should have at least {start, end}
                    # Accept both {start,end,label} and {startTime,endTime,type} forms
                    normalised = []
                    for item in val:
                        if not isinstance(item, dict):
                            continue
                        entry: dict = {}
                        # label / type / name
                        for lk in ("label", "type", "name", "title"):
                            if lk in item:
                                entry["label"] = str(item[lk])
                                break
                        # start time
                        for sk in ("start", "startTime", "start_time", "from", "begin"):
                            if sk in item:
                                entry["start"] = item[sk]
                                break
                        # end time
                        for ek in ("end", "endTime", "end_time", "to", "stop"):
                            if ek in item:
                                entry["end"] = item[ek]
                                break
                        if "start" in entry and "end" in entry:
                            normalised.append(entry)
                    if normalised:
                        result["skip_marks"] = normalised
                        print(f"  [META] skip_marks found via key='{key}' in {label}: {normalised}")
                        break

        # Subtitles
        if "subtitles" not in result:
            for key in SUBTITLE_KEYS:
                val = _extract_json_array(html, key)
                if val:
                    result["subtitles"] = val
                    print(f"  [META] subtitles found via key='{key}' in {label}")
                    break

        if "skip_marks" in result and "subtitles" in result:
            break   # both found — no need to check remaining sources

    if "skip_marks" not in result:
        print("  [META] skip_marks: not found in any source")
    if "subtitles" not in result:
        print("  [META] subtitles: not found in any source")

    return result


# ══════════════════════════════════════════════════════════════════
# SECTION 7 — SINGLE URL PROCESSOR
# ══════════════════════════════════════════════════════════════════

def extract_one(watch_url: str, serial: int) -> dict | None:
    from playwright.sync_api import sync_playwright

    anime_id_m = re.search(r"/watch/(\d+)", watch_url)
    episode_m  = re.search(r"ep=(\d+)",     watch_url)
    anime_id   = anime_id_m.group(1) if anime_id_m else "?"
    episode    = episode_m.group(1)  if episode_m  else "?"

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

        # ── 4. Pull meta from the active iframe ────────────────────
        meta = extract_meta_from_iframe(page)

        page_title = page.title()
        browser.close()

    # ── 5. Build the flat output entry ────────────────────────────
    title = (
        page_title.strip()
        if page_title and page_title.strip()
        else f"Anime {anime_id} – Episode {episode}"
    )

    entry = build_flat_entry(
        serial    = serial,
        title     = title,
        watch_url = watch_url,
        anime_id  = anime_id,
        episode   = episode,
        servers   = servers,
        meta      = meta,
    )

    # ── 6. Summary ────────────────────────────────────────────────
    server_keys = [
        k for k in entry
        if k not in ("serial", "title", "url", "mal_id_with_ep_and_stream_type",
                     "skip_marks", "subtitles")
    ]
    print(f"  ✓ serial={serial}  {len(servers)} DUB server(s)  {len(server_keys)} stream key(s)")
    for k in server_keys:
        print(f"    {k}: {entry[k]}")

    return entry


# ══════════════════════════════════════════════════════════════════
# SECTION 8 — CLI & MAIN
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
