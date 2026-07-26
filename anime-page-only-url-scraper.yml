"""
AniSnatch scraper — GitHub Actions edition
==========================================
Modes (set via env vars by the workflow):

  MODE=full_range   PAGE_START=N  PAGE_END=M
      Scrape pages N–M fresh. Merge results into urls_list.json.

  MODE=daily_update
      1. Scrape pages 1–4 for new anime + updated episode counts.
      2. Re-check every "ongoing" / "aired" entry already in
         urls_list.json — if it now appears in the fresh scrape
         with a higher aired count, update it in-place.
      Save result back to urls_list.json.
"""

import asyncio
import json
import os
import re
import time
from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://anisnatch.top/updated?page="
OUTPUT_FILE = "urls_list.json"

CF_CLEARANCE = os.environ.get("CF_CLEARANCE", "")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

PAGE_DELAY       = 4      # seconds between page loads (be polite)
GOTO_TIMEOUT     = 45000  # ms
SELECTOR_TIMEOUT = 15000  # ms
# ─────────────────────────────────────────────────────────────────────────────

ANIME_PATTERN = re.compile(r'data-href="anime/(\d+)"\s+title="([^"]+)"')
EPS_PATTERN   = re.compile(r'class="tick-item tick-eps">([^<]+)</div>')


# ── Episode parser ────────────────────────────────────────────────────────────

def parse_eps(raw: str):
    """
    Returns (total, aired, status).

    "12/12"  → (12,   12,   "complete")
    "16/24"  → (24,   16,   "aired")
    "15/?"   → (None, 15,   "ongoing")
    "12"     → (12,   12,   "complete")
    ""       → (None, None, "unknown")
    """
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
    # unknown
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
    """Returns list of (anime_id, title, total, aired, status)."""
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
    """
    Scrape pages start..end (inclusive).
    Returns dict: anime_id → (title, total, aired, status)
    On duplicate anime_id, keep the entry with highest aired count.
    """
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


# ── Load / save JSON ──────────────────────────────────────────────────────────

def load_existing() -> dict[str, dict]:
    """Load urls_list.json → dict keyed by anime_id."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        records = json.load(f)
    return {r["anime_id"]: r for r in records if "anime_id" in r}


def save_records(records_by_id: dict[str, dict]):
    """Re-number serials and save."""
    final = []
    for serial, record in enumerate(records_by_id.values(), start=1):
        record["serial_no"] = serial
        final.append(record)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)
    return final


# ── Modes ─────────────────────────────────────────────────────────────────────

async def mode_full_range(browser_page, start: int, end: int):
    print(f"\n  MODE: full_range  pages {start}–{end}")
    scraped = await scrape_pages(browser_page, start, end)

    # Merge into existing JSON (new entries added, existing updated)
    existing = load_existing()
    for anime_id, (title, total, aired, status) in scraped.items():
        existing[anime_id] = build_record(
            existing.get(anime_id, {}).get("serial_no", 0),
            anime_id, title, total, aired, status
        )

    final = save_records(existing)
    print_summary(final, f"full_range pages {start}–{end}")


async def mode_daily_update(browser_page):
    print(f"\n  MODE: daily_update  (pages 1–4 + refresh ongoing/aired)")

    # Step 1: scrape pages 1–4 for new + recently updated anime
    fresh = await scrape_pages(browser_page, 1, 4)

    # Step 2: load existing JSON
    existing = load_existing()

    updated_count = 0
    new_count     = 0

    # Step 3: apply fresh scrape results
    for anime_id, (title, total, aired, status) in fresh.items():
        if anime_id in existing:
            old = existing[anime_id]
            old_aired = old.get("total_ep_aired") or old.get("total_ep")
            new_aired = aired or total
            if new_aired and (old_aired is None or new_aired > old_aired):
                existing[anime_id] = build_record(
                    old["serial_no"], anime_id, title, total, aired, status
                )
                updated_count += 1
        else:
            existing[anime_id] = build_record(
                0, anime_id, title, total, aired, status
            )
            new_count += 1

    print(f"\n  Daily update summary:")
    print(f"    New anime found  : {new_count}")
    print(f"    Records updated  : {updated_count}")

    final = save_records(existing)
    print_summary(final, "daily_update")


# ── Summary printer ───────────────────────────────────────────────────────────

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
    print(f"  Output             : {OUTPUT_FILE}")
    print(f"{'═'*60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    mode       = os.environ.get("MODE", "daily_update")
    page_start = int(os.environ.get("PAGE_START", "1"))
    page_end   = int(os.environ.get("PAGE_END",   "4"))

    print(f"\n{'═'*60}")
    print(f"  AniSnatch Scraper — GitHub Actions")
    print(f"  Mode       : {mode}")
    if mode == "full_range":
        print(f"  Pages      : {page_start} → {page_end}")
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

        if CF_CLEARANCE:
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
