"""
yoga_scraper.py
---------------
Scrapes yoga class schedules from studio websites and saves them
to schedule_data.json for the frontend calendar to read.

Think of this script like a diligent assistant who:
  1. Visits each studio's website
  2. Reads their schedule page
  3. Writes down all the class info in a standard format
  4. Saves it all to one file (schedule_data.json)

The website then just reads that file — no server needed!

SETUP:
  pip install httpx beautifulsoup4

USAGE:
  python yoga_scraper.py
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# ─── Logging setup ────────────────────────────────────────────────────────────
# This prints helpful messages as the script runs, so you can see what's happening
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── File paths ───────────────────────────────────────────────────────────────
CONFIG_FILE = Path("studios_config.json")
OUTPUT_FILE = Path("schedule_data.json")


# ─── Scraper base class ───────────────────────────────────────────────────────
# Think of this like a blank form. Each studio gets its own version of this
# form filled in with site-specific instructions.

class StudioScraper:
    """
    Base class for all studio scrapers.
    Each studio website is different, so you'll create one subclass per studio.
    All subclasses MUST implement the `scrape()` method.
    """

    def __init__(self, name: str, url: str, color: str):
        self.name = name
        self.url = url
        self.color = color
        # httpx is like the Python equivalent of a web browser making a request
        self.client = httpx.Client(timeout=15, follow_redirects=True)

    def fetch_page(self, url: str = None) -> BeautifulSoup | None:
        """
        Downloads a webpage and returns it as a BeautifulSoup object.
        BeautifulSoup lets you navigate HTML like a tree:
          soup.find("div", class_="schedule") → finds the first matching element
          soup.find_all("li") → finds all <li> elements
        """
        target = url or self.url
        try:
            log.info(f"Fetching {target}")
            response = self.client.get(target)
            response.raise_for_status()  # Raises an error if the page didn't load (e.g. 404)
            return BeautifulSoup(response.text, "html.parser")
        except httpx.HTTPError as e:
            log.error(f"Failed to fetch {target}: {e}")
            return None

    def scrape(self) -> list[dict]:
        """
        Override this method in each studio subclass.
        Should return a list of class dicts, each shaped like:
        {
            "id": "unique_string",
            "title": "Class Name",
            "studio": "Studio Name",
            "instructor": "Instructor Name",
            "start": "2025-02-21T09:00:00",   ← ISO 8601 format
            "end":   "2025-02-21T10:00:00",
            "level": "All Levels",
            "spots_left": 8,
            "color": "#c17a5c"
        }
        """
        raise NotImplementedError("Each studio scraper must implement scrape()")

    def make_id(self, raw: str) -> str:
        """Helper to make a simple unique ID from any string."""
        return f"{self.name}-{hash(raw) % 100000}"


# ─── Example Studio Scrapers ──────────────────────────────────────────────────
# ✏️  INSTRUCTIONS FOR YOU:
#
# Each yoga studio website is different. For each studio you want to support:
#   1. Copy the ExampleStudioScraper class below
#   2. Rename it to match the studio (e.g. SunriseYogaScraper)
#   3. Use your browser's DevTools (right-click → Inspect) to find the HTML
#      elements that contain the schedule data
#   4. Update the scrape() method to target those elements
#   5. Add your new scraper to the SCRAPERS dict at the bottom of this file

class ExampleStudioScraper(StudioScraper):
    """
    🔧 TEMPLATE — replace this with real scraping logic for the studio.

    HOW TO FIND THE RIGHT HTML SELECTORS:
    1. Open the studio's schedule page in Chrome
    2. Right-click on a class name → "Inspect"
    3. Look at the HTML structure around it
    4. Note the class names (e.g. <div class="class-item">) or IDs
    5. Use soup.find_all("div", class_="class-item") to grab them all
    """

    def scrape(self) -> list[dict]:
        soup = self.fetch_page()
        if not soup:
            return []

        classes = []

        # ── EXAMPLE: if the schedule is a table ──────────────────────────────
        # Uncomment and adapt this if the studio uses an HTML table:
        #
        # for row in soup.find_all("tr", class_="class-row"):
        #     title = row.find("td", class_="class-name").text.strip()
        #     time_str = row.find("td", class_="class-time").text.strip()
        #     instructor = row.find("td", class_="instructor").text.strip()
        #     # Parse time_str into a datetime object, then format as ISO 8601
        #     # e.g. "Mon Feb 21, 9:00am" → datetime → "2025-02-21T09:00:00"

        # ── EXAMPLE: if classes are in repeating divs ─────────────────────────
        # Uncomment and adapt this if classes are in <div class="class-card">:
        #
        # for card in soup.find_all("div", class_="class-card"):
        #     title = card.find("h3").text.strip()
        #     ...

        log.warning(f"[{self.name}] ExampleStudioScraper has no real scraping logic yet.")
        log.warning(f"[{self.name}] Open {self.url} in your browser, inspect the HTML, and update this class.")

        return classes


class MindBodyStudioScraper(StudioScraper):
    """
    Scraper for studios that use the Mindbody booking platform.
    Many studios embed a Mindbody widget — you can often find a public
    schedule URL like: https://widgets.mindbodyonline.com/...

    🔧 TODO: Replace the site_id with the real Mindbody site ID for this studio.
             You can find it by inspecting the booking widget on the studio's site.
    """

    def scrape(self) -> list[dict]:
        # Mindbody public schedule pages have a consistent structure
        soup = self.fetch_page()
        if not soup:
            return []

        classes = []

        # Mindbody typically uses class="bw-widget__class" for class rows
        for item in soup.find_all("div", class_="bw-widget__class"):
            try:
                title = item.find("span", class_="bw-widget__class-name").text.strip()
                time_raw = item.find("time")["datetime"] if item.find("time") else None
                instructor_el = item.find("span", class_="bw-widget__staff")
                instructor = instructor_el.text.strip() if instructor_el else "TBA"

                if not time_raw:
                    continue

                start_dt = datetime.fromisoformat(time_raw)
                # Mindbody doesn't always give duration; default to 60 min
                from datetime import timedelta
                end_dt = start_dt + timedelta(minutes=60)

                classes.append({
                    "id": self.make_id(f"{title}{time_raw}"),
                    "title": title,
                    "studio": self.name,
                    "instructor": instructor,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "level": "All Levels",
                    "spots_left": None,
                    "color": self.color
                })
            except Exception as e:
                log.warning(f"[{self.name}] Skipped a class due to parse error: {e}")
                continue

        log.info(f"[{self.name}] Found {len(classes)} classes")
        return classes


# ─── Registry: map studio names to their scraper class ────────────────────────
# ✏️  ADD YOUR SCRAPERS HERE.
# The key must exactly match the "name" field in studios_config.json

SCRAPERS: dict[str, type[StudioScraper]] = {
    "Example Yoga Studio": ExampleStudioScraper,
    "Sunrise Wellness Center": MindBodyStudioScraper,
    # "My Real Studio Name": MyRealStudioScraper,  ← add yours here
}


# ─── Main runner ──────────────────────────────────────────────────────────────

def load_config() -> list[dict]:
    """Load studio list from studios_config.json."""
    with open(CONFIG_FILE) as f:
        return json.load(f)["studios"]


def run_all_scrapers(studios: list[dict]) -> list[dict]:
    """
    Loop through each enabled studio, run its scraper, collect all classes.
    This is like sending your assistant to visit each studio and report back.
    """
    all_classes = []

    for studio in studios:
        if not studio.get("enabled", True):
            log.info(f"Skipping disabled studio: {studio['name']}")
            continue

        scraper_class = SCRAPERS.get(studio["name"])
        if not scraper_class:
            log.warning(f"No scraper registered for: {studio['name']} — skipping")
            continue

        scraper = scraper_class(
            name=studio["name"],
            url=studio["url"],
            color=studio.get("color", "#888888")
        )

        try:
            classes = scraper.scrape()
            all_classes.extend(classes)
            log.info(f"✓ {studio['name']}: {len(classes)} classes scraped")
        except Exception as e:
            log.error(f"✗ {studio['name']} scraper crashed: {e}")

    return all_classes


def save_output(classes: list[dict]) -> None:
    """Write scraped classes to schedule_data.json."""
    output = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "classes": classes
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Saved {len(classes)} total classes to {OUTPUT_FILE}")


def main():
    log.info("=== Yoga Scraper Starting ===")
    studios = load_config()
    log.info(f"Loaded {len(studios)} studios from config")

    classes = run_all_scrapers(studios)
    save_output(classes)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
