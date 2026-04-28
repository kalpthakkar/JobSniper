"""
company/google/google_job_detail.py — Google Careers job detail scraper.

This module fetches a single Google job posting page and parses the full
job description, including qualifications, about the job, and responsibilities.

It is intentionally separate from the listing adapter (google.py), which only
extracts summary data (title, location, etc.) from paginated search results.

Usage:
    python google_job_detail.py
    # or import and call fetch_job_detail(url) directly
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger("job_sniper.company.google.detail")

# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class GoogleJobDetail:
    """Structured representation of a single Google job posting."""
    url: str
    qualifications: str = ""       # Parsed from .KwJkGe
    about_the_job: str = ""        # Parsed from .aG5W3
    responsibilities: str = ""     # Parsed from .BDNOWe


# ──────────────────────────────────────────────
# HTML → structured text helpers
# ──────────────────────────────────────────────

def _parse_section(container: Optional[Tag]) -> str:
    """
    Convert a BeautifulSoup Tag into clean, readable plain text.

    Strategy:
      - <p>  → paragraph separated by a blank line
      - <ul> → each <li> becomes a "• item" bullet
      - <ol> → each <li> becomes a "N. item" numbered entry
      - <h3>, <h4> → heading line followed by a blank line
      - Nested structures are handled recursively.

    Returns an empty string when *container* is None.
    """
    if container is None:
        return ""

    lines: list[str] = []

    def walk(node: Tag) -> None:
        """Recursively walk child nodes and emit formatted lines."""
        for child in node.children:
            if not isinstance(child, Tag):
                # Plain text node — skip bare whitespace-only text
                text = child.get_text()
                if text.strip():
                    lines.append(text.strip())
                continue

            tag = child.name

            if tag in ("h3", "h4"):
                heading = child.get_text(strip=True)
                if heading:
                    lines.append(f"\n{heading}")

            elif tag == "p":
                text = child.get_text(separator=" ", strip=True)
                if text:
                    lines.append(f"\n{text}")

            elif tag == "ul":
                for li in child.find_all("li", recursive=False):
                    item = li.get_text(separator=" ", strip=True)
                    if item:
                        lines.append(f"  • {item}")

            elif tag == "ol":
                for idx, li in enumerate(child.find_all("li", recursive=False), start=1):
                    item = li.get_text(separator=" ", strip=True)
                    if item:
                        lines.append(f"  {idx}. {item}")

            else:
                # div, section, span, etc. — recurse into children
                walk(child)

    walk(container)

    # Join and normalise whitespace between sections
    result = "\n".join(lines)
    # Collapse runs of 3+ newlines down to two (one blank line)
    import re
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ──────────────────────────────────────────────
# Fetcher / parser
# ──────────────────────────────────────────────

class GoogleJobDetailFetcher:
    """Fetches and parses a single Google Careers job detail page."""

    # CSS selectors for each section
    SELECTOR_QUALIFICATIONS = ".KwJkGe"
    SELECTOR_ABOUT           = ".aG5W3"
    SELECTOR_RESPONSIBILITIES = ".BDNOWe"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })

    # ── low-level ──────────────────────────────

    def fetch_html(self, url: str) -> Optional[str]:
        """
        GET *url* and return the response body as a string.

        Returns None on any network / HTTP error so callers can handle
        failures gracefully without try/except boilerplate everywhere.
        """
        try:
            logger.debug(f"[google_detail] GET {url}")
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            logger.warning(f"[google_detail] Failed to fetch {url}: {exc}")
            return None

    # ── parsing ────────────────────────────────

    def parse_html(self, html: str, url: str = "") -> GoogleJobDetail:
        """
        Parse *html* and return a populated :class:`GoogleJobDetail`.

        Each section is extracted with its dedicated CSS selector and then
        converted to structured plain text via :func:`_parse_section`.
        """
        soup = BeautifulSoup(html, "html.parser")

        qualifications  = _parse_section(soup.select_one(self.SELECTOR_QUALIFICATIONS))
        about_the_job   = _parse_section(soup.select_one(self.SELECTOR_ABOUT))
        responsibilities = _parse_section(soup.select_one(self.SELECTOR_RESPONSIBILITIES))

        # Refine Qualification:
        qualifications = qualifications.lstrip('info_outline\nX\n')

        # Split Minimum Qualifications:


        return GoogleJobDetail(
            url=url,
            qualifications=qualifications,
            about_the_job=about_the_job,
            responsibilities=responsibilities,
        )

    # ── high-level entry point ─────────────────

    def fetch_job_detail(self, url: str) -> Optional[GoogleJobDetail]:
        """
        Fetch *url* and return a parsed :class:`GoogleJobDetail`, or None
        if the page could not be retrieved.
        """
        html = self.fetch_html(url)
        if html is None:
            return None
        return self.parse_html(html, url=url)


# ──────────────────────────────────────────────
# Pretty printer
# ──────────────────────────────────────────────

def print_job_detail(detail: GoogleJobDetail) -> None:
    """Print all three sections of a :class:`GoogleJobDetail` to stdout."""
    separator = "─" * 72

    print(separator)
    print("QUALIFICATIONS")
    print(separator)
    print(detail.qualifications or "(no qualifications section found)")

    print()
    print(separator)
    print("ABOUT THE JOB")
    print(separator)
    print(detail.about_the_job or "(no about-the-job section found)")

    print()
    print(separator)
    print("RESPONSIBILITIES")
    print(separator)
    print(detail.responsibilities or "(no responsibilities section found)")
    print()


# ──────────────────────────────────────────────
# Main / demo
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s — %(message)s",
    )

    JOB_URL = (
        "https://www.google.com/about/careers/applications/"
        "jobs/results/113000983574782662-customer-engineer-federal-civilian-agencies-google-public-sector"
    )

    fetcher = GoogleJobDetailFetcher()
    detail = fetcher.fetch_job_detail(JOB_URL)

    if detail is None:
        print("ERROR: Could not fetch the job page. Check logs for details.")
    else:
        print_job_detail(detail)