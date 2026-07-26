"""
anisnatch_extract.py — Stream URL Extractor for anisnatch.to
─────────────────────────────────────────────────────────────
- Reads input URLs from:      inputed_urls_list.txt  AND/OR
                              a remote JSON catalogue (JSON_CATALOGUE_URL)
  JSON status → URL field rules:
    "aired"    → "aired_url"
    "ongoing"  → "aired_url"
    "complete" → "url"
- Skips already-done URLs in: already_processed_urls_list.txt
- Logs failed URLs to:        error_faced_urls_list.txt
                              failed_extract_server_urls_list.txt
- Writes output locally to:   streams.json, streams_2.json … (auto-splits at 3 MB)
                              streams.txt, streams_2.txt  … (plain URL list, mirrored)
- Pushes both .json + .txt to a GitHub Repository after each batch.
- Extracts DUB streams; falls back to SUB if DUB is unavailable.
- Batch size controlled by CLI arg: python anisnatch_extract.py --limit 100

════════════════════════════════════════════════════════
  GITHUB REPO SETUP  (one-time, do this before first run)
════════════════════════════════════════════════════════

  Step 1 — Create a Personal Access Token (Classic)
  ──────────────────────────────────────────────────
  1. Go to  https://github.com/settings/tokens
  2. Click  "Generate new token"  →  "Generate new token (classic)"
  3. Give it a note, e.g. "anisnatch-push"
  4. Set Expiration to whatever you want (90 days / no expiry)
  5. Under Scopes tick:  ✅ repo  (full control of private repositories)
  6. Click  "Generate token"
  7. COPY the token immediately (you only see it once).
     It looks like:  ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  8. Paste it as GITHUB_TOKEN below.

  Step 2 — Create the target repository
  ──────────────────────────────────────
  1. Go to  https://github.com/new
  2. Name it e.g.  "anisnatch-streams"
  3. Set it Private (or Public — your choice)
  4. ✅ tick "Add a README file"  (so the repo is initialised and has a main branch)
  5. Click  "Create repository"
  6. Copy the repo name (owner/repo) and paste as GITHUB_REPO below.
     Example:  "myusername/anisnatch-streams"

  Step 3 — Fill in the two constants below, then run:
  ────────────────────────────────────────────────────
      python anisnatch_extract.py --limit 100

  The script will push streams.json + streams.txt (and any split files) to
  the root of that repo after every batch, overwriting old versions.
════════════════════════════════════════════════════════
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
from urllib.request import urlopen, Request
from urllib.error import URLError

# ══════════════════════════════════════════════════════════════════
# No GitHub token needed here.
# The GitHub Actions workflow (.github/workflows/*.yml) handles
# all git commit + push automatically after the script finishes.
# ══════════════════════════════════════════════════════════════════

# ── FILE PATHS ────────────────────────────────────────────────────
INPUT_FILE      = "inputed_urls_list.txt"
PROCESSED_FILE  = "already_processed_urls_list.txt"
ERROR_FILE      = "error_faced_urls_list.txt"
FAILED_URL_FILE = "failed_extract_server_urls_list.txt"
OUTPUT_BASE     = "streams"
OUTPUT_EXT_JSON = ".json"
OUTPUT_EXT_TXT  = ".txt"
MAX_FILE_BYTES  = 3 * 1024 * 1024   # 3 MB per split file
BASE_URL        = "https://anisnatch.to"
IFRAME_BASE     = "https://anisnatch.to/video/"

# ── JSON CATALOGUE SOURCE ─────────────────────────────────────────
# Remote JSON file listing anime entries.
# Set to "" or None to disable; the script will then only use INPUT_FILE.
JSON_CATALOGUE_URL = (
    "https://raw.githubusercontent.com/donkarboy/anisantch_top"
    "/refs/heads/main/anime-page-only-url-scraper.json"
)
# ─────────────────────────────────────────────────────────────────


# (GitHub push is handled by the Actions workflow — no API code needed here)


# ══════════════════════════════════════════════════════════════════
# SECTION 1 — IFRAME URL DECODERS
# ══════════════════════════════════════════════════════════════════

def _decode_allanime_def(hex_token: str) -> dict:
    """AllAnime /def/ — hex XOR 0x06 → JSON → Wix per-quality m3u8 URLs."""
    result = {
        "raw_json": {}, "media_id": "", "secondary_id": "",
        "thumbnail": "", "qualities": [],
        "m3u8_480": "", "m3u8_720": "", "m3u8_1080": "",
        "streamer_id": "", "key": "", "date": "",
    }
    try:
        raw_bytes = bytes.fromhex(hex_token)
    except ValueError as e:
        result["error"] = f"hex decode failed: {e}"; return result

    xored = bytes([b ^ 0x06 for b in raw_bytes])
    try:
        data = json.loads(xored.decode("latin-1"))
    except Exception as e:
        result["error"] = f"JSON parse failed: {e}"; return result

    result.update({
        "raw_json": data, "streamer_id": data.get("streamerId", ""),
        "key": data.get("key", ""), "date": data.get("date", ""),
    })
    parts = [p.strip() for p in data.get("url", "").split(" | ")]
    if len(parts) >= 1: result["media_id"]     = parts[0]
    if len(parts) >= 2: result["secondary_id"] = parts[1]
    if len(parts) >= 3: result["thumbnail"]    = parts[2]
    if len(parts) >= 4: result["qualities"]    = [q.strip() for q in parts[3].split(",") if q.strip()]

    mid = result["media_id"]
    if mid:
        base = f"https://repackager.wixmp.com/video.wixstatic.com/video/{mid}"
        result["m3u8_480"]  = f"{base}/,480p/mp4/file.mp4.urlset/master.m3u8"
        result["m3u8_720"]  = f"{base}/,720p/mp4/file.mp4.urlset/master.m3u8"
        result["m3u8_1080"] = f"{base}/,1080p/mp4/file.mp4.urlset/master.m3u8"
    return result


def _decode_vibeplayer_b64(b64_token: str) -> str:
    """AniVibe /vibeplayer/ — base64 → vivibebe.site URL."""
    padded = b64_token + "=" * (-len(b64_token) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8")
    except Exception as e:
        return f"ERROR: {e}"


def decode_iframe_url(iframe_url: str) -> dict:
    """Master decoder for any https://anisnatch.to/video/* iframe URL."""
    result = {"server_type": "", "token": "", "ep_suffix": "",
               "iframe_url": iframe_url, "extra": {}}

    if not iframe_url.startswith(IFRAME_BASE):
        result["error"] = "not an anisnatch /video/ URL"; return result

    path = iframe_url[len(IFRAME_BASE):]
    ep_m = re.search(r"/(\d+-\d+)$", path)
    if ep_m:
        result["ep_suffix"] = ep_m.group(1)

    first_slash = path.find("/")
    if first_slash == -1:
        result["error"] = "unexpected URL structure"; return result

    server_type = path[:first_slash]
    rest        = path[first_slash + 1:]
    ep_sfx      = result["ep_suffix"]
    token       = rest[:-(len(ep_sfx) + 1)] if ep_sfx and rest.endswith("/" + ep_sfx) else rest

    result["server_type"] = server_type
    result["token"]       = token

    if   server_type == "def":        result["extra"] = _decode_allanime_def(token)
    elif server_type == "vibeplayer": result["extra"] = {"player_url": _decode_vibeplayer_b64(token)}
    elif server_type == "yt-mp4":     result["extra"] = {"yt_key": token}
    elif server_type == "megaplay":   result["extra"] = {"megaplay_id": token.removesuffix("-dub").removesuffix("-sub")}
    elif server_type == "vidwish":    result["extra"] = {"vidwish_id":  token.removesuffix("-dub").removesuffix("-sub")}
    elif server_type == "ok":         result["extra"] = {"ok_video_id": token, "ok_embed_url": f"https://ok.ru/videoembed/{token}"}
    elif server_type == "mp4":        result["extra"] = {"mp4_slug": token}
    elif server_type == "swift":      result["extra"] = {"swift_token": token}
    elif server_type == "anicdn":     result["extra"] = {"anicdn_hash": token}
    else:                             result["extra"] = {"unknown_token": token}

    if "error" in result.get("extra", {}):
        result["error"] = result["extra"]["error"]
    return result


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — FLAT ENTRY BUILDER
# ══════════════════════════════════════════════════════════════════

_SERVER_ORDER = ["def","vibeplayer","yt-mp4","megaplay","vidwish","ok","mp4","swift","anicdn"]


def build_flat_entry(serial, title, watch_url, anime_id, episode, servers, stream_type="dub"):
    decoded_map: dict[str, list] = {}
    for s in servers:
        dec   = decode_iframe_url(s["iframe_url"])
        stype = dec["server_type"] or "unknown"
        decoded_map.setdefault(stype, []).append(dec)

        active_tag = " ← active" if s["active"] else ""
        print(f"  [DECODE] {s['label']}{active_tag}  type={stype}")
        if stype == "def":
            ex = dec.get("extra", {})
            print(f"    allanime_480:  {ex.get('m3u8_480','')}")
            print(f"    allanime_720:  {ex.get('m3u8_720','')}")
            print(f"    allanime_1080: {ex.get('m3u8_1080','')}")
        elif stype == "vibeplayer": print(f"    player_url: {dec['extra'].get('player_url','')}")
        elif stype == "ok":         print(f"    ok_embed:   {dec['extra'].get('ok_embed_url','')}")
        else:                       print(f"    iframe_url: {dec['iframe_url']}")

    entry: dict = {
        "serial": serial,
        "title":  title,
        "url":    watch_url,
        "mal_id_with_ep_and_stream_type": f"{anime_id}/{episode}=={stream_type}",
    }

    for stype in _SERVER_ORDER:
        decs = decoded_map.get(stype, [])
        if not decs: continue
        dec  = decs[0]
        ex   = dec.get("extra", {})
        iurl = dec["iframe_url"]

        if   stype == "def":        entry.update({"allanime_iframe": iurl, "allanime_480": ex.get("m3u8_480",""), "allanime_720": ex.get("m3u8_720",""), "allanime_1080": ex.get("m3u8_1080","")})
        elif stype == "vibeplayer": entry.update({"anivibe_iframe": iurl, "anivibe": ex.get("player_url","")})
        elif stype == "yt-mp4":     entry.update({"aniyt_iframe": iurl,    "aniyt": iurl})
        elif stype == "megaplay":   entry.update({"megaplay_iframe": iurl,  "megaplay": iurl})
        elif stype == "vidwish":    entry.update({"vidwish_iframe": iurl,   "vidwish": iurl})
        elif stype == "mp4":        entry.update({"mp4_iframe": iurl,       "mp4": iurl})
        elif stype == "swift":      entry.update({"swift_iframe": iurl,     "swift": iurl})
        elif stype == "anicdn":     entry.update({"anicdn_iframe": iurl,    "anicdn": iurl})
        elif stype == "ok":         entry.update({"okcdn_iframe": iurl,     "okcdn": ex.get("ok_embed_url","")})
        else:                       entry.update({f"{stype}_iframe": iurl,  stype: iurl})

    return entry


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — SPLIT-FILE MANAGEMENT  (.json + mirrored .txt)
# ══════════════════════════════════════════════════════════════════

def _all_json_files():
    base     = glob.glob(OUTPUT_BASE + OUTPUT_EXT_JSON)
    numbered = sorted(
        glob.glob(f"{OUTPUT_BASE}_*{OUTPUT_EXT_JSON}"),
        key=lambda f: int(re.search(r'_(\d+)\.json$', f).group(1))
        if re.search(r'_(\d+)\.json$', f) else 0,
    )
    return base + numbered

def all_output_files():
    """Return all local output files (json + txt), for reporting."""
    json_files = _all_json_files()
    txt_files  = [f.replace(OUTPUT_EXT_JSON, OUTPUT_EXT_TXT) for f in json_files
                  if os.path.isfile(f.replace(OUTPUT_EXT_JSON, OUTPUT_EXT_TXT))]
    return json_files + txt_files


def load_all_streams():
    merged = []
    for f in _all_json_files():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    merged.extend(data)
        except Exception:
            pass
    return merged


def _current_write_target_json():
    files = _all_json_files()
    if not files:
        return OUTPUT_BASE + OUTPUT_EXT_JSON
    last = files[-1]
    if os.path.getsize(last) >= MAX_FILE_BYTES:
        m   = re.search(r'_(\d+)\.json$', last)
        idx = int(m.group(1)) + 1 if m else 2
        return f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT_JSON}"
    return last


def _txt_path_for(json_path: str) -> str:
    return json_path.replace(OUTPUT_EXT_JSON, OUTPUT_EXT_TXT)


def _rebuild_txt_for(json_path: str):
    """
    Re-write the .txt mirror for a given .json file.
    Each line = one watch URL from that file's entries.
    Format:  <url>  |  <mal_id_with_ep_and_stream_type>  |  <title>
    """
    txt_path = _txt_path_for(json_path)
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            entries = json.load(fh)
        if not isinstance(entries, list):
            return
        lines = []
        for e in entries:
            url   = e.get("url", "")
            mid   = e.get("mal_id_with_ep_and_stream_type", "")
            title = e.get("title", "")
            lines.append(f"{url}  |  {mid}  |  {title}")
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as ex:
        print(f"  [TXT] Could not write {txt_path}: {ex}")


def save_entry_to_file(url: str, entry: dict) -> str:
    target = _current_write_target_json()
    bucket: list = []
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                bucket = json.load(f)
            if not isinstance(bucket, list):
                bucket = []
        except Exception:
            bucket = []

    bucket.append(entry)
    serialised = json.dumps(bucket, indent=2, ensure_ascii=False)

    # If adding this entry would overflow, spill into a new split file
    if len(serialised.encode("utf-8")) > MAX_FILE_BYTES and len(bucket) > 1:
        bucket.pop()
        with open(target, "w", encoding="utf-8") as f:
            json.dump(bucket, f, indent=2, ensure_ascii=False)
        _rebuild_txt_for(target)

        m      = re.search(r'_(\d+)\.json$', target)
        idx    = int(m.group(1)) + 1 if m else 2
        target = f"{OUTPUT_BASE}_{idx}{OUTPUT_EXT_JSON}"
        bucket = [entry]
        serialised = json.dumps(bucket, indent=2, ensure_ascii=False)

    with open(target, "w", encoding="utf-8") as f:
        f.write(serialised)

    _rebuild_txt_for(target)   # keep .txt in sync
    return target


# ══════════════════════════════════════════════════════════════════
# SECTION 4 — PROCESSED / ERROR / FAILED-URL LOGS
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

def mark_failed_url(url: str):
    with open(FAILED_URL_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — INPUT URL LIST  (txt file + remote JSON catalogue)
# ══════════════════════════════════════════════════════════════════

_RANGE_RE = re.compile(r'^(https?://[^\s]+\?ep=)(\d+)\s+to\s+(\d+)\s*$', re.IGNORECASE)


def _expand_line(raw_line: str) -> list[str]:
    """Expand  '…?ep=1 to 12'  into 12 individual episode URLs."""
    m = _RANGE_RE.match(raw_line.strip())
    if not m:
        return [raw_line.strip()]
    base_prefix = m.group(1)
    start, end  = int(m.group(2)), int(m.group(3))
    if start > end:
        print(f"  [WARN] Range {start} > {end}, expanding in reverse")
        return [f"{base_prefix}{ep}" for ep in range(start, end - 1, -1)]
    return [f"{base_prefix}{ep}" for ep in range(start, end + 1)]


def _urls_from_txt() -> list[str]:
    """Load + range-expand URLs from the local txt input file."""
    if not os.path.isfile(INPUT_FILE):
        print(f"[INFO] {INPUT_FILE} not found — skipping txt source")
        return []
    raw_lines = [l.strip() for l in open(INPUT_FILE, encoding="utf-8") if l.strip()]
    print(f"[INFO] {len(raw_lines)} raw line(s) read from {INPUT_FILE}")
    expanded, range_count = [], 0
    for raw in raw_lines:
        chunk = _expand_line(raw)
        if len(chunk) > 1:
            range_count += 1
            print(f"  [RANGE] Expanded → {len(chunk)} URL(s)  ({chunk[0]}  …  {chunk[-1]})")
        expanded.extend(chunk)
    if range_count:
        print(f"[INFO] {range_count} range(s) expanded → {len(expanded)} total URL(s) from txt")
    return expanded


def _urls_from_json_catalogue() -> list[str]:
    """
    Fetch the remote JSON catalogue and extract episode URLs according to status:
      "aired"    → entry["aired_url"]   (range-expanded)
      "ongoing"  → entry["aired_url"]   (range-expanded)
      "complete" → entry["url"]         (range-expanded)
    Entries missing the expected field are warned and skipped.
    """
    if not JSON_CATALOGUE_URL:
        return []

    print(f"[INFO] Fetching JSON catalogue: {JSON_CATALOGUE_URL}")
    try:
        req  = Request(JSON_CATALOGUE_URL, headers={"User-Agent": "anisnatch-extractor/1.0"})
        with urlopen(req, timeout=30) as resp:
            raw  = resp.read().decode("utf-8")
            data = json.loads(raw)
    except URLError as e:
        print(f"[WARN] Could not fetch JSON catalogue: {e}"); return []
    except json.JSONDecodeError as e:
        print(f"[WARN] JSON catalogue is not valid JSON: {e}"); return []

    if not isinstance(data, list):
        print("[WARN] JSON catalogue root is not a list — skipping"); return []

    print(f"[INFO] JSON catalogue: {len(data)} entries")

    expanded = []
    skipped  = 0
    for entry in data:
        if not isinstance(entry, dict):
            skipped += 1; continue

        status = (entry.get("status") or "").strip().lower()

        if status in ("aired", "ongoing"):
            raw_url = entry.get("aired_url", "")
        elif status == "complete":
            raw_url = entry.get("url", "")
        else:
            # Unknown / missing status — try aired_url first, then url
            raw_url = entry.get("aired_url") or entry.get("url", "")
            if raw_url:
                print(f"  [WARN] Unknown status '{status}' for anime_id="
                      f"{entry.get('anime_id','?')} — using '{raw_url}'")
            else:
                print(f"  [WARN] No usable URL for anime_id={entry.get('anime_id','?')} "
                      f"(status='{status}') — skipping")
                skipped += 1; continue

        if not raw_url:
            name = entry.get("anime_name", entry.get("anime_id", "?"))
            print(f"  [WARN] '{status}' entry has no URL for '{name}' — skipping")
            skipped += 1; continue

        # Range-expand the URL (handles "?ep=1 to 12" syntax)
        chunk = _expand_line(raw_url.strip())
        if len(chunk) > 1:
            print(f"  [RANGE JSON] {entry.get('anime_name','?')} → {len(chunk)} ep(s)"
                  f"  ({chunk[0]}  …  {chunk[-1]})")
        expanded.extend(chunk)

    print(f"[INFO] JSON catalogue produced {len(expanded)} URL(s) "
          f"({skipped} entries skipped)")
    return expanded


def load_input_urls() -> list:
    """
    Merge URLs from:
      1. inputed_urls_list.txt  (local, range-expanded)
      2. Remote JSON catalogue  (fetched, range-expanded, status-routed)
    Deduplicate while preserving order (txt URLs come first).
    """
    txt_urls  = _urls_from_txt()
    json_urls = _urls_from_json_catalogue()

    combined = txt_urls + json_urls
    seen, unique = set(), []
    for u in combined:
        if u not in seen:
            seen.add(u); unique.append(u)

    dupes = len(combined) - len(unique)
    if dupes:
        print(f"[INFO] {dupes} duplicate(s) removed across both sources → {len(unique)} unique")
    else:
        print(f"[INFO] {len(unique)} unique URL(s) across both sources")

    if not unique:
        print(f"[ERROR] No input URLs found from any source.")
        sys.exit(1)

    return unique


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — PLAYWRIGHT HELPERS
# ══════════════════════════════════════════════════════════════════

def _select_stream_type(page, stream_type: str) -> bool:
    try:
        page.wait_for_selector("#server-option", timeout=15_000)
    except Exception:
        print(f"  [{stream_type.upper()}] #server-option not found"); return False

    btn = page.query_selector("#serverType")
    if btn and (btn.get_attribute("data-value") or "").lower() == stream_type:
        print(f"  [{stream_type.upper()}] Already active"); return True

    try:
        overlay = page.query_selector("div.partPlayer")
        if overlay:
            page.evaluate("() => { const e=document.querySelector('div.partPlayer'); if(e) e.style.pointerEvents='none'; }")
    except Exception:
        pass

    try:
        page.evaluate("() => { const b=document.querySelector('#serverType'); if(b) b.click(); }")
        time.sleep(0.5)
    except Exception:
        pass

    item = page.query_selector(f'#serverTypeMenu .dropdown-item[data-type="{stream_type}"]')
    if not item:
        print(f"  [{stream_type.upper()}] Not found in #serverTypeMenu"); return False

    try:
        print(f"  [{stream_type.upper()}] Clicking …")
        clicked = page.evaluate(f"""
            () => {{
                const i = document.querySelector('#serverTypeMenu .dropdown-item[data-type="{stream_type}"]');
                if (!i) return false;
                i.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}}));
                return true;
            }}
        """)
        if not clicked: return False
        time.sleep(2.5)
    except Exception as e:
        try:
            item.click(force=True); time.sleep(2.5)
        except Exception as e2:
            print(f"  [{stream_type.upper()}] Click failed: {e2}"); return False

    btn = page.query_selector("#serverType")
    if btn and (btn.get_attribute("data-value") or "").lower() == stream_type:
        print(f"  [{stream_type.upper()}] Confirmed active"); return True
    if page.query_selector(f'#serverTypeMenu .dropdown-item.active[data-type="{stream_type}"]'):
        print(f"  [{stream_type.upper()}] Confirmed active via .active"); return True

    print(f"  [{stream_type.upper()}] Could not confirm selection"); return False


def ensure_best_stream_type(page) -> str | None:
    """Try DUB → fallback SUB → None."""
    if _select_stream_type(page, "dub"):
        return "dub"
    print("  [FALLBACK] DUB unavailable — trying SUB …")
    if _select_stream_type(page, "sub"):
        return "sub"
    print("  [SKIP] Neither DUB nor SUB available")
    return None


def extract_servers_from_dom(page) -> list:
    servers = []
    try:
        for item in page.query_selector_all("#streamTypeMenu .dropdown-item"):
            source = item.get_attribute("data-source") or ""
            if not source: continue
            server   = item.get_attribute("data-server") or ""
            label_el = item.query_selector(".item-text.text-title, .item-text")
            label    = label_el.inner_text().strip() if label_el else server
            info_el  = item.query_selector(".item-info")
            info     = info_el.inner_text().strip() if info_el else ""
            servers.append({
                "server":     server,
                "source":     source,
                "label":      label,
                "info":       info,
                "active":     "active" in (item.get_attribute("class") or ""),
                "iframe_url": urljoin(BASE_URL + "/video/", source),
            })
    except Exception as e:
        print(f"  [DOM] Error reading #streamTypeMenu: {e}")
    return servers


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
            args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"],
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

        try:
            page.goto(watch_url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(2)
        except Exception as e:
            browser.close()
            reason = f"Navigation failed: {e}"
            print(f"  [ERROR] {reason}")
            mark_error(watch_url, reason); mark_failed_url(watch_url)
            return None

        stream_type = ensure_best_stream_type(page)
        if not stream_type:
            browser.close()
            reason = "No DUB or SUB available"
            print(f"  [SKIP] {reason}")
            mark_error(watch_url, reason); mark_failed_url(watch_url)
            return None

        print(f"  [STREAM] Using {stream_type.upper()}")

        servers = extract_servers_from_dom(page)
        if not servers:
            browser.close()
            reason = f"No servers found after {stream_type.upper()} selection"
            print(f"  [ERROR] {reason}")
            mark_error(watch_url, reason); mark_failed_url(watch_url)
            return None

        print(f"  [DOM] Found {len(servers)} {stream_type.upper()} server(s):")
        for s in servers:
            atag = " ← active" if s["active"] else ""
            itag = f" [{s['info']}]" if s["info"] else ""
            print(f"    {s['label']}{itag}  server={s['server']}{atag}")
            print(f"      iframe_url: {s['iframe_url']}")

        page_title = page.title()
        browser.close()

    title = page_title.strip() if page_title and page_title.strip() else f"Anime {anime_id} – Episode {episode}"
    entry = build_flat_entry(serial, title, watch_url, anime_id, episode, servers, stream_type)

    stream_keys = [k for k in entry if k not in ("serial","title","url","mal_id_with_ep_and_stream_type")]
    print(f"  ✓ serial={serial}  {len(servers)} server(s)  {len(stream_keys)} key(s)")
    for k in stream_keys:
        print(f"    {k}: {entry[k]}")

    return entry


# ══════════════════════════════════════════════════════════════════
# SECTION 8 — CLI & MAIN
# ══════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="AniSnatch DUB/SUB stream extractor")
    parser.add_argument(
        "--limit", type=str, default="100",
        help="URLs to process this run: 2|20|50|100|250|500|1000|5000|full  (default: 100)",
    )
    return parser.parse_args()


def resolve_limit(raw: str) -> int | None:
    raw = raw.strip().lower()
    if raw == "full": return None
    try:    return int(raw)
    except: print(f"[WARN] Unrecognised --limit '{raw}', using 100"); return 100


def main():
    args  = parse_args()
    limit = resolve_limit(args.limit)
    limit_label = "full" if limit is None else str(limit)
    print(f"[INFO] Batch limit: {limit_label}\n")

    input_urls  = load_input_urls()
    processed   = load_processed_urls()
    print(f"[INFO] {len(processed)} already processed — skipping")

    all_streams      = load_all_streams()
    existing_serials = [v.get("serial", 0) for v in all_streams if isinstance(v, dict)]
    next_serial      = max(existing_serials, default=0) + 1

    pending = [u for u in input_urls if u not in processed]
    print(f"[INFO] {len(pending)} pending")

    batch = pending[:limit] if limit is not None else pending
    print(f"[INFO] Processing {len(batch)} URL(s) this run\n")

    if not batch:
        print("[INFO] Nothing to do — all URLs already processed.")
        sys.exit(0)

    ok = 0
    errors = 0

    for url in batch:
        existing = next(
            (e for e in all_streams if isinstance(e, dict) and e.get("url") == url), None
        )
        serial = existing["serial"] if existing and "serial" in existing else next_serial
        if serial == next_serial:
            next_serial += 1

        entry = extract_one(url, serial)
        if entry:
            target = save_entry_to_file(url, entry)
            mark_processed(url)
            ok += 1
            print(f"  → Saved to {target}  +  {_txt_path_for(target)}")
        else:
            errors += 1

    print(f"\n{'='*55}")
    print(f"Batch limit   : {limit_label}")
    print(f"Processed     : {ok} succeeded  |  {errors} failed")
    print(f"Output files  : {all_output_files()}")
    print(f"Processed log : {PROCESSED_FILE}")
    print(f"Error log     : {ERROR_FILE}")
    print(f"Failed URLs   : {FAILED_URL_FILE}")
    sys.exit(0)   # always 0 — partial success is not a hard CI failure


if __name__ == "__main__":
    main()
