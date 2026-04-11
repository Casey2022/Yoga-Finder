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
    Opens the Victor Yoga Mindbody page, clicks through every day tab
    for the current week, and optionally navigates forward to scrape
    up to 4 weeks of upcoming classes.
    """
    all_raw = []  # collects (date, raw_classes) tuples across all days

    with sync_playwright() as p:
        log.info("Launching headless Chromium browser...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        })

        log.info(f"Navigating to {STUDIO_URL}")
        page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
        log.info("Page DOM loaded — waiting for React to render...")
        page.wait_for_timeout(5000)

        # ── Dismiss cookie/consent banner via JavaScript ────────────────────────
        # The banner overlays the whole page and blocks all clicks.
        # Using JS to click it directly bypasses the overlay entirely —
        # like reaching through a glass window to press a button.
        try:
            dismissed = page.evaluate("""
                () => {
                    // Try every likely OK/Accept button on the page
                    const selectors = [
                        '#truste-consent-button',
                        '.truste-button-primary',
                        'button[id*="consent"]',
                        'button[class*="consent"]',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) { btn.click(); return sel; }
                    }
                    // Fallback: find any button whose text is "OK" or "Accept"
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const okBtn = buttons.find(b =>
                        ['ok', 'accept', 'agree'].includes(b.innerText.trim().toLowerCase())
                    );
                    if (okBtn) { okBtn.click(); return 'text:' + okBtn.innerText; }
                    return null;
                }
            """)
            if dismissed:
                log.info(f"Cookie banner dismissed via JS ({dismissed})!")
                page.wait_for_timeout(2000)
            else:
                log.info("No cookie banner found — continuing.")
        except Exception as e:
            log.info(f"Cookie banner dismissal skipped: {e}")

        # ── Log what the page looks like right now ─────────────────────────────
        page_text = page.evaluate("() => document.body.innerText.slice(0, 300)")
        log.info(f"Page preview: {page_text[:200]!r}")

        # ── Find day tabs — they are divs, not buttons ─────────────────────────
        # The page preview shows "SAT\n11\nSUN\n12" in the text, but buttons list
        # had none of these. So the tabs are clickable <div> or <span> elements.
        # We find any element whose text matches a day abbreviation + date number.
        day_tab_info = page.evaluate("""
            () => {
                const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                const results = [];
                // Check all elements — divs, spans, li, etc.
                document.querySelectorAll('div, span, li, a').forEach(el => {
                    const t = el.innerText ? el.innerText.trim() : '';
                    // Match patterns like "SAT\\n11" or "WED 15" or just "SAT"
                    const hasDay = days.some(d => t.startsWith(d));
                    const hasNum = /\\d{1,2}/.test(t);
                    const isShort = t.length < 12;
                    if (hasDay && hasNum && isShort) {
                        results.push({
                            tag: el.tagName,
                            text: t,
                            // Store a path we can use to re-find this element
                            index: results.length
                        });
                    }
                });
                // Deduplicate by text
                const seen = new Set();
                return results.filter(r => {
                    if (seen.has(r.text)) return false;
                    seen.add(r.text);
                    return true;
                });
            }
        """)
        log.info(f"Day tabs found: {day_tab_info}")

        # ── Loop through weeks ─────────────────────────────────────────────────
        WEEKS_TO_SCRAPE = 4

        for week_num in range(WEEKS_TO_SCRAPE):
            log.info(f"\n── Week {week_num + 1} of {WEEKS_TO_SCRAPE} ──")

            # Re-fetch day tabs each week since the DOM re-renders
            day_tab_info = page.evaluate("""
                () => {
                    const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                    const results = [];
                    document.querySelectorAll('div, span, li, a').forEach(el => {
                        const t = el.innerText ? el.innerText.trim() : '';
                        const hasDay = days.some(d => t.startsWith(d));
                        const hasNum = /\\d{1,2}/.test(t);
                        const isShort = t.length < 12;
                        if (hasDay && hasNum && isShort) {
                            results.push({ tag: el.tagName, text: t });
                        }
                    });
                    const seen = new Set();
                    return results.filter(r => {
                        if (seen.has(r.text)) return false;
                        seen.add(r.text);
                        return true;
                    });
                }
            """)

            if not day_tab_info:
                log.warning("No day tabs found — scraping current view only.")
                raw = extract_classes_from_page(page)
                all_raw.append((datetime.now(), raw))
                break

            log.info(f"Tabs this week: {[t['text'] for t in day_tab_info]}")

            for tab_index, tab_info in enumerate(day_tab_info):
                try:
                    tab_text = tab_info['text']
                    log.info(f"  Clicking: {tab_text!r}")

                    # ── Click using mouse coordinates ──────────────────────────
                    # JavaScript .click() bypasses React's synthetic event system.
                    # Instead we find the element's screen position and use a real
                    # mouse click — this is what React actually listens for.
                    bbox = page.evaluate(f"""
                        () => {{
                            const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                            const all = Array.from(document.querySelectorAll('div, span, li, a'));
                            const seen = new Set();
                            const tabs = all.filter(el => {{
                                const t = el.innerText ? el.innerText.trim() : '';
                                const hasDay = days.some(d => t.startsWith(d));
                                const hasNum = /\\d{{1,2}}/.test(t);
                                const isShort = t.length < 12;
                                if (hasDay && hasNum && isShort && !seen.has(t)) {{
                                    seen.add(t);
                                    return true;
                                }}
                                return false;
                            }});
                            const el = tabs[{tab_index}];
                            if (!el) return null;
                            const r = el.getBoundingClientRect();
                            return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
                        }}
                    """)

                    if not bbox:
                        log.warning(f"  Could not get bounding box for tab {tab_index} — skipping")
                        continue

                    log.info(f"  Mouse clicking at ({bbox['x']:.0f}, {bbox['y']:.0f})")
                    page.mouse.click(bbox['x'], bbox['y'])
                    page.wait_for_timeout(3000)

                    # Log what the page shows after clicking
                    page_snippet = page.evaluate("""
                        () => document.body.innerText
                            .split('\\n')
                            .map(l => l.trim())
                            .filter(l => l.length > 2)
                            .slice(0, 20)
                            .join(' | ')
                    """)
                    log.info(f"    Page after click: {page_snippet[:250]!r}")

                    # Parse date directly from tab text e.g. "SAT\n11"
                    date = parse_date_from_tab(tab_text)
                    raw = extract_classes_from_page(page)
                    log.info(f"    → {len(raw)} classes on {date.strftime('%a %b %d')}")
                    all_raw.append((date, raw))

                except Exception as e:
                    log.warning(f"  Error on tab {tab_info}: {e}")
                    continue

            # ── Navigate to next week ──────────────────────────────────────────
            if week_num < WEEKS_TO_SCRAPE - 1:
                try:
                    # Log all candidate "next" elements for debugging
                    candidates = page.evaluate("""
                        () => {
                            return Array.from(document.querySelectorAll('*'))
                                .filter(el => {
                                    const t = (el.innerText || '').trim();
                                    const label = (el.getAttribute('aria-label') || '').toLowerCase();
                                    return t === '>' || t === '›' || t === '→' ||
                                           label.includes('next') || label.includes('forward');
                                })
                                .map(el => ({
                                    tag: el.tagName,
                                    text: (el.innerText || '').trim().slice(0, 20),
                                    label: el.getAttribute('aria-label') || ''
                                }))
                                .slice(0, 10);
                        }
                    """)
                    log.info(f"  Next-week candidates: {candidates}")

                    clicked = page.evaluate("""
                        () => {
                            const el = Array.from(document.querySelectorAll('*')).find(el => {
                                const t = (el.innerText || '').trim();
                                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                                return t === '>' || t === '›' || t === '→' ||
                                       label.includes('next') || label.includes('forward');
                            });
                            if (el) { el.click(); return true; }
                            return false;
                        }
                    """)
                    if clicked:
                        page.wait_for_timeout(2500)
                        log.info("  Navigated to next week ✓")
                    else:
                        log.warning("  Could not find next week arrow — stopping.")
                        break
                except Exception as e:
                    log.warning(f"  Next week navigation failed: {e}")
                    break

        # Save debug snapshot of final state
        if debug:
            save_debug_snapshot(page)

        browser.close()
        log.info("\nBrowser closed.")

    # ── Convert all raw results into structured class dicts ────────────────────
    return build_class_list(all_raw)


def extract_classes_from_page(page) -> list[dict]:
    """
    Runs JavaScript in the current browser state to extract class cards.
    Returns a list of raw dicts with lines, time, and duration.
    This is called once per day tab.
    """
    return page.evaluate("""
        () => {
            const results = [];
            const allElements = document.querySelectorAll('div, li, article');

            allElements.forEach(el => {
                const text = el.innerText || '';
                const hasTime = /\\d{1,2}:\\d{2}\\s*(am|pm)/i.test(text);
                const hasBook = el.querySelector('button') !== null ||
                                text.toLowerCase().includes('book');
                const isSmall = text.length < 300;

                if (hasTime && hasBook && isSmall) {
                    const timeMatch = text.match(/(\\d{1,2}:\\d{2}\\s*(?:am|pm)(?:\\s*(?:EDT|EST|CDT|CST|MDT|MST|PDT|PST))?)/i);
                    const time = timeMatch ? timeMatch[1].trim() : null;
                    const durationMatch = text.match(/(\\d+)\\s*min/i);
                    const duration = durationMatch ? parseInt(durationMatch[1]) : 60;
                    const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                    results.push({ raw_text: text.trim(), lines, time, duration });
                }
            });

            const seen = new Set();
            return results.filter(r => {
                if (seen.has(r.raw_text)) return false;
                seen.add(r.raw_text);
                return true;
            });
        }
    """)


def parse_date_from_tab(tab_text: str) -> datetime:
    """
    Parses the date directly from a day tab label like 'SAT\\n11' or 'WED 15'.
    Uses the current month and year since Mindbody only shows the day number.

    This is more reliable than reading the page heading because:
    - The tab text is always visible and consistent
    - The page heading sometimes takes extra time to update after clicking
    """
    try:
        # Extract the day number from text like "SAT\n11" or "MON 13"
        day_num = int(re.search(r'\d{1,2}', tab_text).group())
        today = datetime.now()

        # Build a date with current month — if the day number is much earlier
        # than today, we've wrapped to next month
        candidate = today.replace(day=day_num, hour=0, minute=0, second=0, microsecond=0)

        # If the tab day is before today by more than 3 days, it's next month
        if (candidate - today).days < -3:
            # Advance to next month
            if today.month == 12:
                candidate = candidate.replace(year=today.year + 1, month=1)
            else:
                candidate = candidate.replace(month=today.month + 1)

        return candidate
    except Exception:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def parse_date_from_heading(page, week_offset: int, day_index: int) -> datetime:
    """
    Tries to read the date from the page heading like:
    'Classes on Wednesday, April 2'
    Falls back to calculating the date from today + offsets.
    """
    try:
        heading = page.locator("text=/Classes on /i").first.inner_text(timeout=2000)
        # e.g. "Classes on Wednesday, April 2"
        date_part = re.sub(r"Classes on\s+\w+,\s*", "", heading).strip()
        # Add current year since Mindbody doesn't show it
        parsed = datetime.strptime(f"{date_part} {datetime.now().year}", "%B %d %Y")
        return parsed
    except Exception:
        # Fallback: calculate date as today + week offset days + day index
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        # Find which weekday today is (0=Mon) and go to start of current week
        week_start = today - timedelta(days=today.weekday())
        return week_start + timedelta(weeks=week_offset, days=day_index)

def build_class_list(all_raw: list[tuple]) -> list[dict]:
    """
    Converts (date, raw_classes) tuples from all day tabs into
    clean structured class dicts ready for the calendar.
    """
    SKIP_LINES = {
        "book now", "book", "yoga", "pilates", "barre", "cycling",
        "offerings", "staff", "about the studio", "highlights",
        "amenities", "location", "customer reviews", "load more",
        "in studio", "show all"
    }

    def is_noise(line: str) -> bool:
        l = line.strip().lower()
        if l in SKIP_LINES:
            return True
        if line.startswith("$"):
            return True
        if re.match(r'^\d+ (min|hr)', l):
            return True
        if re.match(r'classes on .+', l):
            return True
        if re.match(r'\d{1,2}:\d{2}', l):
            return True
        return False

    def looks_like_name(line: str) -> bool:
        parts = line.strip().split()
        if len(parts) < 2 or len(parts) > 4:
            return False
        if line.isupper():
            return False
        if len(line) > 40:
            return False
        return True

    classes = []
    seen_keys = set()

    for date, raw_list in all_raw:
        for i, item in enumerate(raw_list):
            try:
                lines = item.get('lines', [])
                time_str = item.get('time')
                duration = item.get('duration', 60)

                if not time_str or not lines:
                    continue

                clean_lines = [l for l in lines if not is_noise(l) and len(l) > 2]
                if not clean_lines:
                    continue

                title = max(clean_lines, key=len)
                if title.lower() in ("offerings", "about the studio"):
                    continue

                instructor = next(
                    (l for l in clean_lines if l != title and looks_like_name(l)),
                    "TBA"
                )

                start_dt = parse_time(time_str, date)
                if not start_dt:
                    continue
                end_dt = start_dt + timedelta(minutes=duration)

                # Deduplicate across all days
                key = f"{title}|{start_dt.isoformat()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                classes.append({
                    "id":         f"{STUDIO_NAME}-{hash(key) % 100000}",
                    "title":      title,
                    "studio":     STUDIO_NAME,
                    "instructor": instructor,
                    "start":      start_dt.isoformat(),
                    "end":        end_dt.isoformat(),
                    "level":      "All Levels",
                    "spots_left": None,
                    "color":      STUDIO_COLOR
                })

            except Exception as e:
                log.warning(f"Skipped item: {e}")
                continue

    # Sort by start time so the calendar looks clean
    classes.sort(key=lambda c: c["start"])
    log.info(f"\nTotal unique classes extracted: {len(classes)}")
    return classes


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
