"""
AniSnatch scraper — GitHub Actions edition
==========================================
MODE=full_range   PAGE_START=N  PAGE_END=M  →  scrape pages N–M, merge into JSON files
MODE=daily_update                            →  scrape pages 1–4, refresh ongoing/aired entries

Output files:
  anime-page-only-url-scraper.json        (first chunk, always exists)
  anime-page-only-url-scraper-part2.json  (if total > 3 MB)
  anime-page-only-url-scraper-part3.json  ...and so on

Each file is kept under MAX_FILE_BYTES. Records are split across files in
serial_no order; serial_no is global and continuous across all parts.
"""

import asyncio
import json
import os
import re
from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL      = "https://anisnatch.top/updated?page="
OUTPUT_BASE   = "anime-page-only-url-scraper"   # no .json — added below
MAX_FILE_BYTES = 3 * 1024 * 1024                 # 3 MB per file

# ⚠️  Replace this value whenever the cookie expires (every 1-2 days).
CF_CLEARANCE = (
    "D._WYCCEfl8eiaDEatFPOa7ylkYoABNnm8s9Nc1xKY0-1785064175-1.2.1.1-"
    "HmhSN8yb.lJGZyHwsKhHNElZTG4cosHNSfsvlv8qdF7ZS7CGzuXxEcbkZ9jKUm"
    "SwSYgMbpnHvKk9wU7t93GgGoBSO1xzaHosvw0jh4vmDeBxaz2nkjkv97wg36n.Ma"
    "zNDUtwowwu24Bw_2IhVncWK4pRmh4LG55K1VjGwRMZYj5p3R6KIG_zOq4bGhwiFA"
    "8b4tyl00XtUWkwJjQBnGLGCRNPScjBfMrt_wlz_oTbQIrm8QQ9QdvvdQaG0kL.RH"
    "LRWsNlyFqlpap0Hfi8RJgkI3br9O.f_TqWXcyP_NNDbmNJSzMvT1g_uvvohusrcc"
    "QjqzxILhBgYbosrrkyr35OkCxn_vvxa4c3wyyqhLiqonk"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

PAGE_DELAY       = 4      # seconds between page loads
GOTO_TIMEOUT     = 45000  # ms
SELECTOR_TIMEOUT = 15000  # ms
# ─────────────────────────────────────────────────────────────────────────────

ANIME_PATTERN = re.compile(r'data-href="anime/(\d+)"\s+title="([^"]+)"')
EPS_PATTERN   = re.compile(r'class="tick-item tick-eps">([^<]+)</div>')


# ── File name helpers ─────────────────────────────────────────────────────────

def part_filename(part: int) -> str:
    """
    part=1 → anime-page-only-url-scraper.json
    part=2 → anime-page-only-url-scraper-part2.json
    part=3 → anime-page-only-url-scraper-part3.json
    """
    if part == 1:
        return f"{OUTPUT_BASE}.json"
    return f"{OUTPUT_BASE}-part{part}.json"


def all_existing_part_files() -> list[str]:
    """Return sorted list of all part files that exist on disk."""
    files = []
    part = 1
    while True:
        fn = part_filename(part)
        if os.path.exists(fn):
            files.append(fn)
            part += 1
        else:
            break
    return files


# ── Split-aware save ──────────────────────────────────────────────────────────

def save_all_records(records: list[dict]):
    """
    Re-number serials globally, then split into ≤1 MB part files.
    Deletes any old part files that are no longer needed.
    """
    # Re-number serials
    for i, r in enumerate(records, start=1):
        r["serial_no"] = i

    # Split into chunks ≤ MAX_FILE_BYTES
    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    current_size = len("[\n]")   # empty array baseline

    for record in records:
        # Measure this record as it would appear in JSON
        record_json = json.dumps(record, ensure_ascii=False)
        # +2 for ",\n" separator
        record_size = len(record_json.encode("utf-8")) + 2

        if current_chunk and current_size + record_size > MAX_FILE_BYTES:
            chunks.append(current_chunk)
            current_chunk = []
            current_size  = len("[\n]")

        current_chunk.append(record)
        current_size += record_size

    if current_chunk:
        chunks.append(current_chunk)

    if not chunks:
        chunks = [[]]   # always write at least one (empty) file

    # Write part files
    for part, chunk in enumerate(chunks, start=1):
        fn = part_filename(part)
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(chunk, f, indent=2, ensure_ascii=False)
        size_kb = os.path.getsize(fn) / 1024
        print(f"  💾  {fn}  ({len(chunk)} records, {size_kb:.1f} KB)")

    # Remove stale part files from a previous run that had more parts
    stale_part = len(chunks) + 1
    while os.path.exists(part_filename(stale_part)):
        fn = part_filename(stale_part)
        os.remove(fn)
        print(f"  🗑️  removed stale file: {fn}")
        stale_part += 1


# ── Load all existing records (across all parts) ──────────────────────────────

def load_existing() -> dict[str, dict]:
    """Load all part files → dict keyed by anime_id."""
    all_records: dict[str, dict] = {}
    for fn in all_existing_part_files():
        with open(fn, encoding="utf-8") as f:
            records = json.load(f)
        for r in records:
            if "anime_id" in r:
                all_records[r["anime_id"]] = r
    return all_records


# ── Episode parser ────────────────────────────────────────────────────────────

def parse_eps(raw: str):
    raw = raw.strip()
    if not raw:
        return None, None, "unknown"
    if '/' in raw:
        aired_str, total_str = raw.split('/', 1)
        aired = int(aired_str) if aired_str.isdigit() else None
        total = int(total_str) if total_str.isdigit() else None
        if total is None:
            return None, aired, "ongoing"
        if aired == total:
            return total, total, "complete"
        return total, aired, "aired"
    ep = int(raw) if raw.isdigit() else None
    if ep:
        return ep, ep, "complete"
    return None, None, "unknown"


# ── Record builder ────────────────────────────────────────────────────────────

def build_record(serial: int, anime_id: str, title: str,
                 total, aired, status: str) -> dict:
    base = f"https://anisnatch.top/watch/{anime_id}"

    def ep_url(n):
        if not n or n == 1:
            return f"{base}?ep=1"
        return f"{base}?ep=1 to {n}"

    if status == "complete":
        return {
            "serial_no":  serial,
            "anime_id":   anime_id,
            "anime_name": title,
            "total_ep":   total,
            "status":     "complete",
            "url":        ep_url(total),
        }
    if status == "aired":
        return {
            "serial_no":      serial,
            "anime_id":       anime_id,
            "anime_name":     title,
            "total_ep":       total,
            "total_ep_aired": aired,
            "status":         "aired",
            "aired_url":      ep_url(aired),
            "url":            ep_url(total),
        }
    if status == "ongoing":
        return {
            "serial_no":      serial,
            "anime_id":       anime_id,
            "anime_name":     title,
            "total_ep_aired": aired,
            "status":         "ongoing",
            "aired_url":      ep_url(aired),
        }
    return {
        "serial_no":  serial,
        "anime_id":   anime_id,
        "anime_name": title,
        "total_ep":   "unknown",
        "status":     "unknown",
        "url":        f"{base}?ep=1",
    }


# ── Page parser ───────────────────────────────────────────────────────────────

def parse_updated_page(html: str):
    blocks = re.split(r'(?=<li\s+ani-id=)', html)
    seen: set[str] = set()
    results = []
    for block in blocks:
        am = ANIME_PATTERN.search(block)
        if not am:
            continue
        anime_id = am.group(1)
        if anime_id in seen:
            continue
        seen.add(anime_id)
        title = am.group(2)
        em = EPS_PATTERN.search(block)
        total, aired, status = parse_eps(em.group(1) if em else "")
        results.append((anime_id, title, total, aired, status))
    return results


# ── Browser helper ────────────────────────────────────────────────────────────

async def safe_goto(page, url: str, selector: str | None = None):
    for attempt in range(1, 4):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            if selector:
                try:
                    await page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT)
                except Exception:
                    pass
            return await page.content()
        except Exception as exc:
            print(f"    ⚠ attempt {attempt}/3 failed for {url}: {type(exc).__name__}")
            if attempt < 3:
                await asyncio.sleep(6 * attempt)
    return ""


async def scrape_pages(page, start: int, end: int) -> dict[str, tuple]:
    collected: dict[str, tuple] = {}
    for page_num in range(start, end + 1):
        html    = await safe_goto(page, f"{BASE_URL}{page_num}", "li[ani-id]")
        entries = parse_updated_page(html)
        new_count = 0
        for anime_id, title, total, aired, status in entries:
            if anime_id not in collected:
                collected[anime_id] = (title, total, aired, status)
                new_count += 1
            else:
                _, _, old_aired, _ = collected[anime_id]
                if aired is not None and (old_aired is None or aired > old_aired):
                    collected[anime_id] = (title, total, aired, status)
        print(f"  [Page {page_num:>3}/{end}]  {len(entries):>2} entries  "
              f"+{new_count} new  |  total scraped: {len(collected):>4}")
        await asyncio.sleep(PAGE_DELAY)
    return collected


# ── Modes ─────────────────────────────────────────────────────────────────────

async def mode_full_range(browser_page, start: int, end: int):
    print(f"\n  MODE: full_range  pages {start}–{end}")
    scraped  = await scrape_pages(browser_page, start, end)
    existing = load_existing()

    for anime_id, (title, total, aired, status) in scraped.items():
        serial = existing.get(anime_id, {}).get("serial_no", 0)
        existing[anime_id] = build_record(serial, anime_id, title, total, aired, status)

    records = list(existing.values())
    save_all_records(records)
    print_summary(records, f"full_range pages {start}–{end}")


async def mode_daily_update(browser_page):
    print(f"\n  MODE: daily_update  (pages 1–4 + refresh ongoing/aired)")
    fresh    = await scrape_pages(browser_page, 1, 4)
    existing = load_existing()

    updated_count = 0
    new_count     = 0

    for anime_id, (title, total, aired, status) in fresh.items():
        if anime_id in existing:
            old       = existing[anime_id]
            old_aired = old.get("total_ep_aired") or old.get("total_ep")
            new_aired = aired or total
            if new_aired and (old_aired is None or new_aired > old_aired):
                existing[anime_id] = build_record(
                    old["serial_no"], anime_id, title, total, aired, status
                )
                updated_count += 1
        else:
            existing[anime_id] = build_record(0, anime_id, title, total, aired, status)
            new_count += 1

    print(f"\n  Daily update summary:")
    print(f"    New anime found  : {new_count}")
    print(f"    Records updated  : {updated_count}")

    records = list(existing.values())
    save_all_records(records)
    print_summary(records, "daily_update")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(records: list, label: str):
    counts = {"complete": 0, "aired": 0, "ongoing": 0, "unknown": 0}
    for r in records:
        counts[r.get("status", "unknown")] += 1
    print(f"\n{'═'*60}")
    print(f"  ✅  {label} done")
    print(f"  Total records      : {len(records)}")
    print(f"  Complete           : {counts['complete']}")
    print(f"  Partially aired    : {counts['aired']}")
    print(f"  Ongoing (no total) : {counts['ongoing']}")
    print(f"  Unknown            : {counts['unknown']}")
    print(f"{'═'*60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    mode       = os.environ.get("MODE",       "daily_update")
    page_start = int(os.environ.get("PAGE_START", "1"))
    page_end   = int(os.environ.get("PAGE_END",   "4"))

    print(f"\n{'═'*60}")
    print(f"  AniSnatch Scraper — GitHub Actions")
    print(f"  Mode : {mode}")
    if mode == "full_range":
        print(f"  Pages: {page_start} → {page_end}")
    print(f"{'═'*60}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "accept-language": "en-US,en;q=0.9",
            },
        )
        await context.add_cookies([{
            "name":     "cf_clearance",
            "value":    CF_CLEARANCE,
            "domain":   "anisnatch.top",
            "path":     "/",
            "httpOnly": False,
            "secure":   True,
            "sameSite": "None",
        }])

        page = await context.new_page()
        await page.route("**/*", lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_()
        )

        if mode == "full_range":
            await mode_full_range(page, page_start, page_end)
        else:
            await mode_daily_update(page)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
