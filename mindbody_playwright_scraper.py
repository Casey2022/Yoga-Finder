"""
mindbody_playwright_scraper.py
------------------------------
Scrapes yoga class schedules from ALL Mindbody studios listed in
studios_config.json using Playwright — a library that controls a
real browser from Python.

WHY PLAYWRIGHT INSTEAD OF httpx?
─────────────────────────────────
Think of httpx like sending a letter to a website asking for its content.
Mindbody ignores that letter because it only speaks to real browsers.

Playwright is like hiring a person to sit at a computer, open Chrome,
wait for the page to fully load, then read what's on screen and report back.
It's slower, but it can handle any website a human could visit.

SETUP (run these once in your terminal):
─────────────────────────────────────────
  pip install playwright
  playwright install chromium

USAGE:
──────
  # Normal run — scrapes all studios and saves to schedule_data.json
  python mindbody_playwright_scraper.py

  # Debug run — saves screenshot + HTML for the first studio only
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
CONFIG_FILE  = Path("studios_config.json")
OUTPUT_FILE  = Path("schedule_data.json")
PAGE_TIMEOUT = 20_000  # 20 seconds


# ── Debug helpers ──────────────────────────────────────────────────────────────

def save_debug_snapshot(page, studio_name: str) -> None:
    """Saves a screenshot and raw HTML for inspection."""
    Path("debug").mkdir(exist_ok=True)
    safe_name = re.sub(r'[^a-z0-9]', '-', studio_name.lower())
    page.screenshot(path=f"debug/{safe_name}-screenshot.png", full_page=True)
    Path(f"debug/{safe_name}-page.html").write_text(page.content(), encoding="utf-8")
    log.info(f"Debug snapshot saved to debug/{safe_name}-*")


# ── Per-studio scraper ─────────────────────────────────────────────────────────

def scrape_mindbody_studio(
    studio_name: str,
    studio_url: str,
    studio_color: str,
    debug: bool = False
) -> list[dict]:
    """
    Scrapes a single Mindbody studio page — clicks through all day tabs
    for 4 weeks and extracts class data.

    This function is called once per studio in studios_config.json.
    Think of it as sending one dedicated researcher per studio.
    """
    log.info(f"\n{'='*60}")
    log.info(f"  Scraping: {studio_name}")
    log.info(f"  URL:      {studio_url}")
    log.info(f"{'='*60}")

    all_raw = []  # (date, raw_classes) tuples across all days

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        })

        log.info(f"Navigating to {studio_url}")
        page.goto(studio_url, wait_until="domcontentloaded", timeout=60_000)
        log.info("Page DOM loaded — waiting for React to render...")
        page.wait_for_timeout(5000)

        # ── Dismiss cookie/consent banner ──────────────────────────────────
        try:
            dismissed = page.evaluate("""
                () => {
                    const selectors = [
                        '#truste-consent-button',
                        '.truste-button-primary',
                        'button[id*="consent"]',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) { btn.click(); return sel; }
                    }
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const okBtn = buttons.find(b =>
                        ['ok','accept','agree'].includes(b.innerText.trim().toLowerCase())
                    );
                    if (okBtn) { okBtn.click(); return 'text:' + okBtn.innerText; }
                    return null;
                }
            """)
            if dismissed:
                log.info(f"Cookie banner dismissed ({dismissed})")
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # ── Wait for schedule content ──────────────────────────────────────
        # Different studios use different indicators that the page is ready.
        # We try a few common ones then proceed regardless.
        try:
            page.wait_for_selector(
                "button:has-text('Book'), button:has-text('Register'), "
                "[class*='session'], [class*='class-name'], .bw-session",
                timeout=15_000
            )
            log.info("Schedule content detected!")
        except PlaywrightTimeout:
            log.info("Schedule selector timed out — proceeding anyway.")

        # ── Detect widget type ─────────────────────────────────────────────
        # Mindbody Branded Web (on studio's own site) shows a compact
        # calendar grid. We detect it by looking for "Find a Class" heading.
        is_branded_widget = page.evaluate("""
            () => {
                const text = document.body.innerText;
                return text.includes('Find a Class') &&
                       (text.includes('Full Calendar') || text.includes('My Account'));
            }
        """)
        log.info(f"Branded widget detected: {is_branded_widget}")

        # ── Log page state ─────────────────────────────────────────────────
        page_text = page.evaluate("() => document.body.innerText.slice(0, 200)")
        log.info(f"Page preview: {page_text[:120]!r}")

        # ── Branded widget: scrape by clicking calendar dates ──────────────
        if is_branded_widget:
            log.info("Using branded widget scraping strategy...")
            all_raw = scrape_branded_widget(page, studio_url, all_raw)
            if debug:
                save_debug_snapshot(page, studio_name)
            browser.close()
            log.info(f"\nBrowser closed for {studio_name}.")
            return build_class_list(all_raw, studio_name, studio_color, studio_url)

        # Give slower studios a bit more time to fully render
        page.wait_for_timeout(3000)

        # ── Find day tabs ──────────────────────────────────────────────────
        # Strategy A: standard "MON\n13" format used by most studios
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

        # Strategy B: numbered tabs — some Mindbody pages show just dates like
        # "13", "14", "15" in a horizontal row. Detect by finding 7 consecutive
        # small number elements in the same parent.
        if not day_tab_info:
            log.info("Standard day tabs not found — trying numbered tab fallback...")
            day_tab_info = page.evaluate("""
                () => {
                    // Find all small elements containing only 1-2 digit numbers
                    const candidates = Array.from(
                        document.querySelectorAll('div, span, button, li')
                    ).filter(el => {
                        const t = (el.innerText || '').trim();
                        return /^\\d{1,2}$/.test(t);
                    });

                    if (candidates.length < 5) return [];

                    // Check if they're siblings (same parent = a tab row)
                    const parentGroups = {};
                    candidates.forEach(el => {
                        const key = el.parentElement ? el.parentElement.outerHTML.slice(0,150) : 'none';
                        if (!parentGroups[key]) parentGroups[key] = [];
                        parentGroups[key].push(el);
                    });

                    // Find the group with the most consecutive numbers
                    let best = [];
                    Object.values(parentGroups).forEach(group => {
                        if (group.length > best.length) best = group;
                    });

                    if (best.length < 5) return [];

                    // Convert to our standard format: just use the number as text
                    return best.slice(0, 7).map(el => ({
                        tag: el.tagName,
                        text: (el.innerText || '').trim(),
                        isNumbered: true
                    }));
                }
            """)
            if day_tab_info:
                log.info(f"Found {len(day_tab_info)} numbered tabs: {[t['text'] for t in day_tab_info]}")

        log.info(f"Day tabs found: {[t['text'] for t in day_tab_info]}")

        # ── Loop through 4 weeks ───────────────────────────────────────────
        WEEKS_TO_SCRAPE = 4

        # Track whether this studio uses numbered tabs (detected above)
        uses_numbered_tabs = bool(day_tab_info and day_tab_info[0].get('isNumbered'))

        for week_num in range(WEEKS_TO_SCRAPE):
            log.info(f"\n── Week {week_num + 1} of {WEEKS_TO_SCRAPE} ──")

            # Re-fetch tabs using whichever strategy worked initially
            if uses_numbered_tabs:
                day_tab_info = page.evaluate("""
                    () => {
                        const candidates = Array.from(
                            document.querySelectorAll('div, span, button, li')
                        ).filter(el => /^\\d{1,2}$/.test((el.innerText || '').trim()));
                        if (candidates.length < 5) return [];
                        const parentGroups = {};
                        candidates.forEach(el => {
                            const key = el.parentElement ? el.parentElement.outerHTML.slice(0,150) : 'none';
                            if (!parentGroups[key]) parentGroups[key] = [];
                            parentGroups[key].push(el);
                        });
                        let best = [];
                        Object.values(parentGroups).forEach(g => {
                            if (g.length > best.length) best = g;
                        });
                        if (best.length < 5) return [];
                        return best.slice(0, 7).map(el => ({
                            tag: el.tagName,
                            text: (el.innerText || '').trim(),
                            isNumbered: true
                        }));
                    }
                """)
            else:
                day_tab_info = page.evaluate("""
                    () => {
                        const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                        const results = [];
                        document.querySelectorAll('div, span, li, a').forEach(el => {
                            const t = el.innerText ? el.innerText.trim() : '';
                            const hasDay = days.some(d => t.startsWith(d));
                            const hasNum = /\\d{1,2}/.test(t);
                            if (hasDay && hasNum && t.length < 12) {
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
                # Still try numbered fallback if standard didn't work
                if not day_tab_info:
                    day_tab_info = page.evaluate("""
                        () => {
                            const candidates = Array.from(
                                document.querySelectorAll('div, span, button, li')
                            ).filter(el => /^\\d{1,2}$/.test((el.innerText || '').trim()));
                            if (candidates.length < 5) return [];
                            const parentGroups = {};
                            candidates.forEach(el => {
                                const key = el.parentElement ? el.parentElement.outerHTML.slice(0,150) : 'none';
                                if (!parentGroups[key]) parentGroups[key] = [];
                                parentGroups[key].push(el);
                            });
                            let best = [];
                            Object.values(parentGroups).forEach(g => {
                                if (g.length > best.length) best = g;
                            });
                            if (best.length < 5) return [];
                            return best.slice(0, 7).map(el => ({
                                tag: el.tagName,
                                text: (el.innerText || '').trim(),
                                isNumbered: true
                            }));
                        }
                    """)

            if not day_tab_info:
                log.warning("No day tabs found — scraping current view only.")
                raw = extract_classes_from_page(page, studio_url)
                all_raw.append((datetime.now(), raw))
                break

            log.info(f"Tabs: {[t['text'] for t in day_tab_info]}")

            for tab_index, tab_info in enumerate(day_tab_info):
                try:
                    tab_text  = tab_info['text']
                    is_numbered = tab_info.get('isNumbered', False)
                    log.info(f"  Clicking: {tab_text!r}")

                    # Use mouse coordinates for real React click events.
                    # The JS selector differs for numbered vs named tabs.
                    if is_numbered:
                        bbox = page.evaluate(f"""
                            () => {{
                                const candidates = Array.from(
                                    document.querySelectorAll('div, span, button, li')
                                ).filter(el => /^\\d{{1,2}}$/.test((el.innerText || '').trim()));
                                const parentGroups = {{}};
                                candidates.forEach(el => {{
                                    const key = el.parentElement ? el.parentElement.outerHTML.slice(0,150) : 'none';
                                    if (!parentGroups[key]) parentGroups[key] = [];
                                    parentGroups[key].push(el);
                                }});
                                let best = [];
                                Object.values(parentGroups).forEach(g => {{
                                    if (g.length > best.length) best = g;
                                }});
                                const el = best.slice(0, 7)[{tab_index}];
                                if (!el) return null;
                                const r = el.getBoundingClientRect();
                                return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
                            }}
                        """)
                    else:
                        bbox = page.evaluate(f"""
                            () => {{
                                const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                                const all = Array.from(document.querySelectorAll('div, span, li, a'));
                                const seen = new Set();
                                const tabs = all.filter(el => {{
                                    const t = el.innerText ? el.innerText.trim() : '';
                                    const hasDay = days.some(d => t.startsWith(d));
                                    const hasNum = /\\d{{1,2}}/.test(t);
                                    if (hasDay && hasNum && t.length < 12 && !seen.has(t)) {{
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
                        continue

                    page.mouse.click(bbox['x'], bbox['y'])
                    page.wait_for_timeout(3000)

                    date = parse_date_from_tab(tab_text)
                    raw  = extract_classes_from_page(page, studio_url)
                    log.info(f"    → {len(raw)} classes on {date.strftime('%a %b %d')}")
                    all_raw.append((date, raw))

                except Exception as e:
                    log.warning(f"  Error on tab {tab_info}: {e}")
                    continue

            # ── Navigate to next week ──────────────────────────────────────
            if week_num < WEEKS_TO_SCRAPE - 1:
                try:
                    clicked = page.evaluate("""
                        () => {
                            const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
                            const all = Array.from(document.querySelectorAll('div, span, li, a'));
                            const seen = new Set();
                            const tabs = all.filter(el => {
                                const t = el.innerText ? el.innerText.trim() : '';
                                const hasDay = days.some(d => t.startsWith(d));
                                const hasNum = /\\d{1,2}/.test(t);
                                if (hasDay && hasNum && t.length < 12 && !seen.has(t)) {
                                    seen.add(t);
                                    return true;
                                }
                                return false;
                            });
                            if (!tabs.length) return false;
                            const lastTab = tabs[tabs.length - 1];
                            const lastRect = lastTab.getBoundingClientRect();
                            const candidates = Array.from(
                                document.querySelectorAll('div, span, button, a, svg, path')
                            ).filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.left > lastRect.right &&
                                       Math.abs(r.top - lastRect.top) < 60 &&
                                       r.width < 80 && r.height < 80 &&
                                       r.width > 0 && r.height > 0;
                            });
                            if (candidates.length > 0) {
                                candidates[0].click();
                                return true;
                            }
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

        if debug:
            save_debug_snapshot(page, studio_name)

        browser.close()
        log.info(f"\nBrowser closed for {studio_name}.")

    return build_class_list(all_raw, studio_name, studio_color, studio_url)


def scrape_branded_widget(page, studio_url: str, all_raw: list) -> list:
    """
    Scrapes studios that use the Mindbody Branded Web widget on their own site.
    These show a compact S M T W T F S calendar grid with numbered dates.
    We click each date in the grid to load that day's classes.

    The analogy: this is like flipping through a paper desk calendar,
    clicking each date square to see what's scheduled.
    """
    WEEKS_TO_SCRAPE = 4

    for week_num in range(WEEKS_TO_SCRAPE):
        log.info(f"\n── Week {week_num + 1} of {WEEKS_TO_SCRAPE} (branded widget) ──")

        # Find all clickable date numbers in the calendar grid.
        # These are typically <td> or <div> elements containing 1-2 digit numbers
        # that are NOT greyed out (past dates are often dimmed).
        date_cells = page.evaluate("""
            () => {
                // Find elements that look like calendar date cells:
                // short text, just a number, inside a table or grid
                const cells = Array.from(
                    document.querySelectorAll('td, th, [role="gridcell"], .bw-widget__cal-day')
                ).filter(el => {
                    const t = (el.innerText || '').trim();
                    if (!/^\\d{1,2}$/.test(t)) return false;
                    // Skip obviously past/disabled dates
                    const style = window.getComputedStyle(el);
                    const opacity = parseFloat(style.opacity);
                    if (!isNaN(opacity) && opacity < 0.4) return false;
                    return true;
                });

                const seen = new Set();
                return cells.filter(el => {
                    const t = (el.innerText || '').trim();
                    if (seen.has(t)) return false;
                    seen.add(t);
                    return true;
                }).map(el => {
                    const r = el.getBoundingClientRect();
                    return {
                        text: (el.innerText || '').trim(),
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        visible: r.width > 0 && r.height > 0
                    };
                }).filter(d => d.visible && parseInt(d.text) > 0);
            }
        """)

        if not date_cells:
            log.warning("No date cells found in branded widget calendar.")
            break

        log.info(f"Date cells: {[d['text'] for d in date_cells]}")

        for cell in date_cells:
            try:
                log.info(f"  Clicking date: {cell['text']}")
                page.mouse.click(cell['x'], cell['y'])
                page.wait_for_timeout(2000)

                date = parse_date_from_tab(cell['text'])
                raw  = extract_branded_classes(page, studio_url)
                log.info(f"    → {len(raw)} classes on {date.strftime('%a %b %d')}")
                all_raw.append((date, raw))

            except Exception as e:
                log.warning(f"  Error clicking date {cell['text']}: {e}")
                continue

        # Navigate to next week — look for a ">" or "›" button near the calendar
        if week_num < WEEKS_TO_SCRAPE - 1:
            try:
                clicked = page.evaluate("""
                    () => {
                        // Find next-month/next-week arrow near the calendar
                        const candidates = Array.from(
                            document.querySelectorAll('button, a, td, span, div')
                        ).filter(el => {
                            const t = (el.innerText || '').trim();
                            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                            return t === '>' || t === '›' || t === '→' || t === '»' ||
                                   aria.includes('next') || aria.includes('forward');
                        });
                        if (!candidates.length) return false;
                        candidates[0].click();
                        return true;
                    }
                """)
                if clicked:
                    page.wait_for_timeout(2000)
                    log.info("  Navigated to next week ✓")
                else:
                    log.info("  No next arrow found — one week of data collected.")
                    break
            except Exception as e:
                log.warning(f"  Navigation error: {e}")
                break

    return all_raw


def extract_branded_classes(page, studio_url: str) -> list[dict]:
    """
    Extracts class data from the Mindbody Branded Web widget.
    These widgets show classes in a clean list: time, name, instructor, button.
    """
    return page.evaluate(f"""
        () => {{
            const results = [];
            const STUDIO_URL = '{studio_url}';

            // Classes in the branded widget are typically in rows with
            // class name, time, instructor, and a Book/Register button
            const rows = document.querySelectorAll(
                '.bw-session, [class*="session"], [class*="class-row"], ' +
                '[class*="ClassCard"], li, article'
            );

            // Also try finding by time pattern + button combo
            const allEls = document.querySelectorAll('div, li, article, section');
            allEls.forEach(el => {{
                const text = el.innerText || '';
                const hasTime = /\\d{{1,2}}:\\d{{2}}\\s*(am|pm|AM|PM)/i.test(text);
                const hasAction = /book|register|waitlist|sign.?up/i.test(text);
                const isSmall = text.length > 10 && text.length < 400;

                if (hasTime && hasAction && isSmall) {{
                    const timeMatch = text.match(
                        /(\\d{{1,2}}:\\d{{2}}\\s*(?:AM|PM)(?:\\s*[-–]\\s*\\d{{1,2}}:\\d{{2}}\\s*(?:AM|PM))?(?:\\s*EDT|EST|CDT|CST|MDT|MST|PDT|PST)?)/i
                    );
                    const time = timeMatch ? timeMatch[1].trim() : null;
                    const durationMatch = text.match(/(\\d+)\\s*min/i);
                    const duration = durationMatch ? parseInt(durationMatch[1]) : 60;
                    const lines = text.split('\\n')
                        .map(l => l.trim()).filter(l => l.length > 0);

                    let booking_url = STUDIO_URL;
                    el.querySelectorAll('a[href], button[onclick]').forEach(a => {{
                        if (a.href && (a.href.includes('book') || a.href.includes('mindbody')))
                            booking_url = a.href;
                    }});

                    results.push({{ raw_text: text.trim(), lines, time, duration, booking_url }});
                }}
            }});

            const seen = new Set();
            return results.filter(r => {{
                if (seen.has(r.raw_text)) return false;
                seen.add(r.raw_text);
                return true;
            }});
        }}
    """)


# ── JavaScript extraction ──────────────────────────────────────────────────────

def extract_classes_from_page(page, studio_url: str) -> list[dict]:
    """Runs JS in the browser to extract class cards from the current view."""
    return page.evaluate(f"""
        () => {{
            const results = [];
            const STUDIO_URL = '{studio_url}';
            const allElements = document.querySelectorAll('div, li, article');

            allElements.forEach(el => {{
                const text = el.innerText || '';
                const hasTime = /\\d{{1,2}}:\\d{{2}}\\s*(am|pm)/i.test(text);
                const hasBook = el.querySelector('button') !== null ||
                                text.toLowerCase().includes('book');
                const isSmall = text.length < 300;

                if (hasTime && hasBook && isSmall) {{
                    const timeMatch = text.match(/(\\d{{1,2}}:\\d{{2}}\\s*(?:am|pm)(?:\\s*(?:EDT|EST|CDT|CST|MDT|MST|PDT|PST))?)/i);
                    const time = timeMatch ? timeMatch[1].trim() : null;
                    const durationMatch = text.match(/(\\d+)\\s*min/i);
                    const duration = durationMatch ? parseInt(durationMatch[1]) : 60;
                    const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                    let booking_url = STUDIO_URL;
                    const links = el.querySelectorAll('a[href]');
                    links.forEach(a => {{
                        const href = a.href || '';
                        if (href.includes('mindbody') || href.includes('book')) {{
                            booking_url = href;
                        }}
                    }});

                    results.push({{ raw_text: text.trim(), lines, time, duration, booking_url }});
                }}
            }});

            const seen = new Set();
            return results.filter(r => {{
                if (seen.has(r.raw_text)) return false;
                seen.add(r.raw_text);
                return true;
            }});
        }}
    """)


# ── Date parsing ───────────────────────────────────────────────────────────────

def parse_date_from_tab(tab_text: str) -> datetime:
    """
    Parses date from tab text.
    Handles both 'MON\\n13' format AND plain numbered '13' format.
    """
    try:
        day_num = int(re.search(r'\d{1,2}', tab_text).group())
        today   = datetime.now()
        candidate = today.replace(day=day_num, hour=0, minute=0, second=0, microsecond=0)
        # If day is more than 3 days in the past, it must be next month
        if (candidate - today).days < -3:
            if today.month == 12:
                candidate = candidate.replace(year=today.year + 1, month=1)
            else:
                candidate = candidate.replace(month=today.month + 1)
        return candidate
    except Exception:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def parse_time(time_str: str | None, base_date: datetime = None) -> datetime | None:
    """
    Converts a time string into a datetime object.
    Handles single times like '9:30 AM' and ranges like '4:30 PM – 5:15 PM'.
    The em dash (–) separator is used by the Mindbody branded widget.
    """
    if not time_str:
        return None

    # Handle time ranges like "4:30 PM – 5:15 PM" or "4:30 PM - 5:15 PM"
    # Split on em dash, en dash, or hyphen and use only the start time
    range_split = re.split(r'\s*[–—-]\s*', time_str)
    if len(range_split) >= 2:
        time_str = range_split[0].strip()

    time_str = re.sub(r'\s*(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\s*', '', time_str).strip()
    try:
        return datetime.fromisoformat(time_str)
    except ValueError:
        pass
    formats = ["%I:%M %p", "%I:%M%p", "%H:%M", "%I:%M %P"]
    today = base_date or datetime.now().replace(second=0, microsecond=0)
    for fmt in formats:
        try:
            parsed = datetime.strptime(time_str.strip(), fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except ValueError:
            continue
    log.warning(f"Could not parse time: '{time_str}'")
    return None


def parse_duration_from_range(time_str: str) -> int:
    """
    Extracts duration in minutes from a time range like '4:30 PM – 5:15 PM'.
    Returns 60 as default if parsing fails.
    """
    try:
        parts = re.split(r'\s*[–—-]\s*', time_str)
        if len(parts) < 2:
            return 60
        fmt_options = ["%I:%M %p", "%I:%M%p", "%I:%M %P"]
        start_str = re.sub(r'\s*(EDT|EST|CDT|CST|MDT|MST|PDT|PST)', '', parts[0]).strip()
        end_str   = re.sub(r'\s*(EDT|EST|CDT|CST|MDT|MST|PDT|PST)', '', parts[1]).strip()
        for fmt in fmt_options:
            try:
                t1 = datetime.strptime(start_str, fmt)
                t2 = datetime.strptime(end_str, fmt)
                mins = int((t2 - t1).total_seconds() / 60)
                return mins if mins > 0 else 60
            except ValueError:
                continue
    except Exception:
        pass
    return 60


# ── Class list builder ─────────────────────────────────────────────────────────

def build_class_list(
    all_raw: list[tuple],
    studio_name: str,
    studio_color: str,
    studio_url: str
) -> list[dict]:
    """Converts raw (date, items) tuples into clean class dicts."""

    SKIP_LINES = {
        "book now", "book", "yoga", "pilates", "barre", "cycling",
        "offerings", "staff", "about the studio", "highlights",
        "amenities", "location", "customer reviews", "load more",
        "in studio", "show all", "drop-in"
    }

    def is_noise(line: str) -> bool:
        l = line.strip().lower()
        if l in SKIP_LINES: return True
        if line.startswith("$"): return True
        if re.match(r'^\d+ (min|hr)', l): return True
        if re.match(r'classes on .+', l): return True
        # Only strip bare time strings, not full lines that are just a time range
        if re.match(r'^\d{1,2}:\d{2}\s*(am|pm)\s*$', l): return True
        return False

    def looks_like_name(line: str) -> bool:
        parts = line.strip().split()
        if len(parts) < 2 or len(parts) > 4: return False
        if line.isupper(): return False
        if len(line) > 40: return False
        return True

    classes   = []
    seen_keys = set()

    for date, raw_list in all_raw:
        for item in raw_list:
            try:
                lines    = item.get('lines', [])
                time_str = item.get('time')
                duration = item.get('duration', 60)

                # If the time field contains a range like "4:30 PM – 5:15 PM",
                # calculate the real duration from it instead of using the default
                if time_str and ('–' in time_str or '—' in time_str):
                    duration = parse_duration_from_range(time_str)

                if not time_str or not lines:
                    continue

                clean = [l for l in lines if not is_noise(l) and len(l) > 2]
                if not clean:
                    continue

                title = max(clean, key=len)
                if title.lower() in ("offerings", "about the studio"):
                    continue

                instructor = next(
                    (l for l in clean if l != title and looks_like_name(l)), "TBA"
                )

                start_dt = parse_time(time_str, date)
                if not start_dt:
                    continue
                end_dt = start_dt + timedelta(minutes=duration)

                key = f"{studio_name}|{title}|{start_dt.isoformat()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                classes.append({
                    "id":          f"{studio_name}-{hash(key) % 100000}",
                    "title":       title,
                    "studio":      studio_name,
                    "instructor":  instructor,
                    "start":       start_dt.isoformat(),
                    "end":         end_dt.isoformat(),
                    "level":       "All Levels",
                    "spots_left":  None,
                    "color":       studio_color,
                    "booking_url": item.get("booking_url", studio_url)
                })
            except Exception as e:
                log.warning(f"Skipped item: {e}")
                continue

    classes.sort(key=lambda c: c["start"])
    log.info(f"Extracted {len(classes)} unique classes for {studio_name}")
    return classes


# ── Output ─────────────────────────────────────────────────────────────────────

def save_all_output(all_classes: list[dict], scraped_studios: list[str]) -> None:
    """
    Merges freshly scraped classes with existing data for other studios
    that are still listed in studios_config.json.

    This prevents stale test data (studios that were removed from the
    config) from lingering in schedule_data.json forever.
    """
    existing = {"classes": []}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)

    # Read the full list of valid studios from the config
    # Any studio NOT in this list is treated as stale and removed
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    valid_studios = {s["name"] for s in config["studios"]}

    # Keep classes only if:
    #   (a) they belong to a studio we didn't scrape this run AND
    #   (b) that studio is still in the config (not stale test data)
    kept = [
        c for c in existing["classes"]
        if c["studio"] not in scraped_studios and c["studio"] in valid_studios
    ]
    merged = kept + all_classes
    merged.sort(key=lambda c: c["start"])

    output = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "classes": merged
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"\nSaved {len(merged)} total classes to {OUTPUT_FILE}")
    log.info(f"  ({len(all_classes)} new + {len(kept)} from other studios)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape all Mindbody studios in config")
    parser.add_argument("--debug", action="store_true",
                        help="Save screenshot + HTML for each studio")
    args = parser.parse_args()

    log.info("=== Mindbody Multi-Studio Scraper Starting ===")

    # Load studio list from config
    with open(CONFIG_FILE) as f:
        config = json.load(f)

    studios = [s for s in config["studios"]
               if s.get("enabled", True) and s.get("platform", "Mindbody") == "Mindbody"]

    log.info(f"Found {len(studios)} enabled Mindbody studios to scrape")

    all_classes     = []
    scraped_studios = []

    for studio in studios:
        try:
            classes = scrape_mindbody_studio(
                studio_name=studio["name"],
                studio_url=studio["url"],
                studio_color=studio.get("color", "#c17a5c"),
                debug=args.debug
            )
            all_classes.extend(classes)
            scraped_studios.append(studio["name"])
            log.info(f"✓ {studio['name']}: {len(classes)} classes")
        except Exception as e:
            log.error(f"✗ {studio['name']} failed: {e}")
            continue

    if all_classes:
        save_all_output(all_classes, scraped_studios)
    else:
        log.warning("No classes extracted from any studio.")

    log.info("\n=== Done ===")


if __name__ == "__main__":
    main()
