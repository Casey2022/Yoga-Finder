"""
mindbody_playwright_scraper.py
------------------------------
Scrapes yoga class schedules from Mindbody pages using Playwright —
a library that controls a real browser from Python.

WHY PLAYWRIGHT INSTEAD OF httpx?
─────────────────────────────────
Think of httpx like sending a letter to a website asking for its content.
Mindbody ignores that letter because it only speaks to real browsers.

Playwright is like hiring a person to sit at a computer, open Chrome,
wait for the page to fully load, then read what's on screen and report back.
It's slower, but it can handle any website a human could visit.

SETUP (run these once in your terminal):
─────────────────────────────────────────
  pip install playwright beautifulsoup4
  playwright install chromium

USAGE:
──────
  # Normal run — scrapes and saves to schedule_data.json
  python mindbody_playwright_scraper.py

  # Debug run — saves a screenshot + raw HTML so you can inspect the page
  python mindbody_playwright_scraper.py --debug
"""

import json
import argparse
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
STUDIO_NAME  = "Victor Yoga Studio"
# Use the main studio page — we'll click the Schedule tab programmatically
STUDIO_URL   = "https://www.mindbodyonline.com/explore/locations/victor-yoga-studio"
STUDIO_COLOR = "#c17a5c"
OUTPUT_FILE  = Path("schedule_data.json")

# How long to wait for the page to load before giving up (milliseconds)
PAGE_TIMEOUT = 20_000  # 20 seconds


# ── Debug helpers ──────────────────────────────────────────────────────────────

def save_debug_snapshot(page) -> None:
    """
    Saves a screenshot and the raw HTML of whatever Playwright currently sees.
    Run with --debug flag to use this.

    This is your best friend when the scraper isn't finding elements —
    it lets you see exactly what the browser loaded.
    """
    Path("debug").mkdir(exist_ok=True)
    page.screenshot(path="debug/screenshot.png", full_page=True)
    Path("debug/page_source.html").write_text(page.content(), encoding="utf-8")
    log.info("Debug snapshot saved to debug/screenshot.png and debug/page_source.html")
    log.info("Open the screenshot to see what the browser loaded.")
    log.info("Open page_source.html and search for class names to find the right selectors.")


# ── Scraper ────────────────────────────────────────────────────────────────────

def scrape_victor_yoga(debug: bool = False) -> list[dict]:
    """
    Opens the Victor Yoga Mindbody page in a real (headless) browser,
    waits for the schedule to load, then extracts class data.

    ┌─────────────────────────────────────────────────────────────────┐
    │  HOW TO FIND THE RIGHT SELECTORS FOR THIS PAGE                  │
    │                                                                 │
    │  1. Open https://www.mindbodyonline.com/explore/locations/      │
    │        victor-yoga-studio in Chrome                             │
    │  2. Wait for the schedule to fully load                         │
    │  3. Right-click a class name → Inspect                          │
    │  4. Look at the HTML around it                                  │
    │  5. Find a pattern — e.g. every class is in a                   │
    │        <div class="bw-widget__class"> or similar                │
    │  6. Update the selectors below to match                         │
    └─────────────────────────────────────────────────────────────────┘
    """
    classes = []

    with sync_playwright() as p:
        log.info("Launching headless Chromium browser...")

        # headless=True means the browser runs invisibly in the background
        # Set headless=False temporarily if you want to WATCH the browser work
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Pretend to be a normal Chrome browser so the site doesn't block us
        page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        })

        log.info(f"Navigating to {STUDIO_URL}")
        page.goto(STUDIO_URL, timeout=PAGE_TIMEOUT)

        # ── Dismiss cookie/consent banner ──────────────────────────────────────
        # The TrustArc cookie banner blocks all clicks until dismissed.
        # We click "OK" to accept and get it out of the way.
        try:
            log.info("Dismissing cookie consent banner...")
            ok_button = page.locator("#truste-consent-button, text=OK, text=Accept").first
            ok_button.click(timeout=5000)
            page.wait_for_timeout(1000)
            log.info("Cookie banner dismissed!")
        except Exception:
            log.info("No cookie banner found (or already dismissed) — continuing.")

        # ── Wait for schedule section to appear ────────────────────────────────
        try:
            log.info("Waiting for schedule content to load...")
            page.wait_for_selector(
                "[class*='Offering'], [class*='ClassCard'], [class*='ScheduleItem'], button:has-text('Book')",
                timeout=PAGE_TIMEOUT
            )
            log.info("Schedule content detected!")
        except PlaywrightTimeout:
            log.warning("Timed out waiting for schedule selector.")
            log.warning("Run with --debug to see what actually loaded.")

        # Optional: scroll to load any lazy-loaded content
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Save debug snapshot if requested
        if debug:
            save_debug_snapshot(page)

        # ── Extract schedule data using JavaScript ─────────────────────────────
        # Rather than parsing HTML with BeautifulSoup, we use Playwright to run
        # JavaScript directly inside the browser. This is more reliable because
        # it queries the live DOM exactly as the browser sees it.
        #
        # The analogy: instead of reading a printed photo of a menu, we're
        # asking the waiter directly what's available tonight.
        log.info("Extracting schedule data via JavaScript...")
        raw_classes = page.evaluate("""
            () => {
                const results = [];

                // Strategy: find all elements that contain a time string
                // (like "5:30pm" or "9:00am") AND a Book button nearby.
                // This targets schedule cards and avoids review items.

                // Look for any element whose text contains a time pattern
                const allElements = document.querySelectorAll('div, li, article, section');

                allElements.forEach(el => {
                    const text = el.innerText || '';
                    const hasTime = /\\d{1,2}:\\d{2}\\s*(am|pm|AM|PM)/i.test(text);
                    const hasBook = el.querySelector('button') !== null ||
                                    text.toLowerCase().includes('book');
                    const isSmall = text.length < 300; // avoid huge containers

                    if (hasTime && hasBook && isSmall) {
                        // Extract time string
                        const timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*(?:am|pm)(?:\\s*(?:EDT|EST|CDT|CST|MDT|MST|PDT|PST))?)/i);
                        const time = timeMatch ? timeMatch[1].trim() : null;

                        // Extract duration if present (e.g. "60 min")
                        const durationMatch = text.match(/(\\d+)\\s*min/i);
                        const duration = durationMatch ? parseInt(durationMatch[1]) : 60;

                        // Split text into lines, filter out empty ones
                        const lines = text.split('\\n')
                                          .map(l => l.trim())
                                          .filter(l => l.length > 0);

                        results.push({
                            raw_text: text.trim(),
                            lines: lines,
                            time: time,
                            duration: duration
                        });
                    }
                });

                // Deduplicate by raw_text
                const seen = new Set();
                return results.filter(r => {
                    if (seen.has(r.raw_text)) return false;
                    seen.add(r.raw_text);
                    return true;
                });
            }
        """)

        browser.close()
        log.info(f"Browser closed. Found {len(raw_classes)} raw schedule candidates.")

    # ── Convert raw JS results into structured class dicts ─────────────────────
    classes = []
    today = datetime.now().replace(second=0, microsecond=0)

    # Lines we always want to skip — noise from the page layout
    SKIP_LINES = {
        "book now", "book", "yoga", "pilates", "barre", "cycling",
        "offerings", "staff", "about the studio", "highlights",
        "amenities", "location", "customer reviews", "load more",
        "in studio", "show all"
    }

    def is_noise(line: str) -> bool:
        """Returns True if this line is a UI label, button, or category tag."""
        l = line.strip().lower()
        if l in SKIP_LINES:
            return True
        if line.startswith("$"):           # price like "$20.00"
            return True
        if re.match(r'^\d+ (min|hr)', l):  # duration like "60 min"
            return True
        if re.match(r'classes on .+', l):  # date header like "Classes on Wednesday..."
            return True
        if re.match(r'\d{1,2}:\d{2}', l): # time string
            return True
        return False

    def looks_like_name(line: str) -> bool:
        """Returns True if this line looks like a person's name (e.g. 'Mandy W')."""
        parts = line.strip().split()
        if len(parts) < 2 or len(parts) > 4:
            return False
        if line.isupper():   # skip ALL CAPS labels
            return False
        if len(line) > 40:   # too long to be a name
            return False
        return True

    for i, item in enumerate(raw_classes):
        try:
            lines = item.get('lines', [])
            time_str = item.get('time')
            duration = item.get('duration', 60)

            if not time_str or not lines:
                continue

            # Filter out noise lines — what's left should be class name + instructor
            clean_lines = [l for l in lines if not is_noise(l) and len(l) > 2]

            if not clean_lines:
                log.warning(f"No usable lines after filtering for item {i} — skipping")
                continue

            # Title: longest remaining line (class names are descriptive)
            title = max(clean_lines, key=len)

            # Instructor: short line that looks like a name, different from title
            instructor = next(
                (l for l in clean_lines if l != title and looks_like_name(l)),
                "TBA"
            )

            # Skip section headers that slipped through
            if title.lower() in ("offerings", "about the studio"):
                continue

            start_dt = parse_time(time_str, today)
            if not start_dt:
                continue
            end_dt = start_dt + timedelta(minutes=duration)

            classes.append({
                "id":         f"{STUDIO_NAME}-{i}-{hash(title + str(start_dt)) % 100000}",
                "title":      title,
                "studio":     STUDIO_NAME,
                "instructor": instructor,
                "start":      start_dt.isoformat(),
                "end":        end_dt.isoformat(),
                "level":      "All Levels",
                "spots_left": None,
                "color":      STUDIO_COLOR
            })
            log.info(f"  ✓ {title} at {time_str} with {instructor}")

        except Exception as e:
            log.warning(f"Skipped item due to error: {e}")
            continue

    # Deduplicate — same title + start time = same class
    seen = set()
    unique = []
    for c in classes:
        key = f"{c['title']}|{c['start']}"
        if key not in seen:
            seen.add(key)
            unique.append(c)

    log.info(f"Extracted {len(unique)} unique classes (from {len(classes)} candidates)")
    return unique


# ── Time parsing ───────────────────────────────────────────────────────────────

def parse_time(time_str: str | None, base_date: datetime = None) -> datetime | None:
    """
    Converts a time string from the page into a Python datetime object.
    base_date is used when only a time (no date) is found on the page.
    """
    if not time_str:
        return None

    # Strip timezone suffixes like EDT, EST, PDT etc.
    time_str = re.sub(r'\s*(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\s*', '', time_str).strip()

    try:
        return datetime.fromisoformat(time_str)
    except ValueError:
        pass

    formats = [
        "%I:%M %p",   # "9:00 AM"
        "%I:%M%p",    # "9:00AM"
        "%H:%M",      # "09:00"
        "%I:%M %P",   # "9:00 am"
    ]

    today = base_date or datetime.now().replace(second=0, microsecond=0)

    for fmt in formats:
        try:
            parsed = datetime.strptime(time_str.strip(), fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except ValueError:
            continue

    log.warning(f"Could not parse time string: '{time_str}'")
    log.warning("Add this format to the formats list in parse_time()")
    return None


# ── Save output ────────────────────────────────────────────────────────────────

def save_output(classes: list[dict]) -> None:
    """Merge new classes with any existing ones from other studios."""
    existing = {"classes": []}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)

    # Remove old entries for this studio, add fresh ones
    other_studios = [c for c in existing["classes"] if c["studio"] != STUDIO_NAME]
    merged = other_studios + classes

    output = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "classes": merged
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {len(merged)} total classes to {OUTPUT_FILE}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape Victor Yoga via Playwright")
    parser.add_argument("--debug", action="store_true",
                        help="Save screenshot + HTML for selector inspection")
    args = parser.parse_args()

    if args.debug:
        log.info("=== DEBUG MODE — will save screenshot and HTML snapshot ===")

    log.info("=== Victor Yoga Playwright Scraper Starting ===")
    classes = scrape_victor_yoga(debug=args.debug)

    if classes:
        save_output(classes)
    else:
        log.warning("No classes were extracted. Check the debug output.")

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
