"""
company/microsoft/microsoft.py — Microsoft Careers API adapter

Fetches jobs from Microsoft's careers API, filters by last 6 hours,
and extracts job details from individual job pages.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger("job_sniper.microsoft")

BASE_URL = "https://apply.careers.microsoft.com/careers"
API_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
DETAIL_API_URL = "https://apply.careers.microsoft.com/api/pcsx/position_details"


class MicrosoftAdapter:
    """Adapter for Microsoft Careers API."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self._csrf_token = None

    def _get_csrf_token(self) -> str:
        """Fetch CSRF token from main careers page."""
        if self._csrf_token:
            return self._csrf_token

        try:
            response = self.session.get(BASE_URL, timeout=self.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            csrf = soup.find("meta", {"name": "_csrf"})
            if csrf and csrf.get("content"):
                self._csrf_token = csrf["content"]
                return self._csrf_token
        except Exception as e:
            logger.warning(f"[microsoft] Failed to fetch CSRF token: {e}")

        return ""

    def _get_offset(self, page: int, page_size: int = 10) -> int:
        """Calculate API offset from page number."""
        if page < 1:
            raise ValueError("Page number must be >= 1")
        return (page - 1) * page_size

    def fetch_page(self, page: int = 1, max_retries: int = 3) -> dict:
        """
        Fetch a single page of jobs from Microsoft API.
        
        Args:
            page: Page number (1-indexed)
            max_retries: Max retry attempts for rate limiting
            
        Returns:
            Dict with 'positions' list and 'count' total
        """
        csrf_token = self._get_csrf_token()
        if not csrf_token:
            logger.warning("[microsoft] No CSRF token available")
            return {"positions": [], "count": 0}

        payload = {
            "domain": "microsoft.com",
            "query": "",
            "location": "",
            "start": self._get_offset(page),
            "sort_by": "timestamp",
            "hl": "en"
        }

        headers = {
            "Accept": "application/json",
            "Referer": BASE_URL,
            "x-csrf-token": csrf_token,
            "x-browser-request-time": str(time.time())
        }

        for attempt in range(max_retries):
            try:
                response = self.session.get(
                    API_URL,
                    headers=headers,
                    params=payload,
                    timeout=self.timeout
                )
                response.raise_for_status()

                data = response.json()
                if data.get("status") == 200 and data.get("data"):
                    return data["data"]

                return {"positions": [], "count": 0}

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"[microsoft] Rate limited (429) on page {page}. "
                            f"Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait_time)
                        continue
                raise
            except Exception as e:
                logger.error(f"[microsoft] Error fetching page {page}: {e}")
                return {"positions": [], "count": 0}

        return {"positions": [], "count": 0}

    def fetch_all_recent_jobs(self, hours: int = 6) -> Tuple[List[dict], int]:
        """
        Fetch all jobs posted within last N hours.
        
        Args:
            hours: Time window in hours
            
        Returns:
            Tuple of (recent_jobs_list, total_count)
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        logger.info(f"[microsoft] Fetching jobs from last {hours} hours (cutoff: {cutoff_time})")

        all_jobs = []
        page = 1
        jobs_outside_window = 0
        page_has_recent = False

        while True:
            try:
                page_data = self.fetch_page(page)
                positions = page_data.get("positions", [])
                total_count = page_data.get("count", 0)

                if not positions:
                    logger.info(
                        f"[microsoft] Reached end of results at page {page}. "
                        f"Fetched {len(all_jobs)} recent jobs from {total_count} total"
                    )
                    break

                page_has_recent = False
                jobs_outside_window = 0

                for job in positions:
                    posted_ts = job.get("postedTs")
                    if not posted_ts:
                        continue

                    # Convert epoch timestamp to datetime
                    posted_dt = datetime.fromtimestamp(posted_ts, tz=timezone.utc)

                    if posted_dt > cutoff_time:
                        all_jobs.append(job)
                        page_has_recent = True
                        jobs_outside_window = 0
                    else:
                        jobs_outside_window += 1

                recent_count = len(all_jobs)
                logger.info(
                    f"[microsoft] Page {page}: fetched {len(positions)} jobs, "
                    f"{recent_count} total in recent window"
                )

                # Early exit: if entire page is outside window, all remaining will be too
                if not page_has_recent and jobs_outside_window >= 10:
                    logger.info(
                        f"[microsoft] Page {page}: All {jobs_outside_window} jobs are outside "
                        f"time window. Stopping early (jobs likely sorted by date)"
                    )
                    break

                page += 1

            except Exception as e:
                logger.error(f"[microsoft] Error during pagination: {e}")
                break

        logger.info(f"[microsoft] Fetched {len(all_jobs)} recent jobs from {total_count} total")
        return all_jobs, total_count

    @staticmethod
    def extract_location(job: dict) -> str:
        """Extract and format location from job data."""
        locations = job.get("locations", [])
        if locations:
            return "; ".join(locations)
        return "Unknown"

    @staticmethod
    def is_remote(job: dict) -> bool:
        """Detect if job is remote."""
        mode = job.get("workLocationOption", "").lower()
        return mode == "remote"


class MicrosoftJobDetailFetcher:
    """Fetches detailed job information from Microsoft job pages."""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _get_csrf_token(self, position_id: str) -> str:
        """Get CSRF token from job detail page."""
        try:
            url = f"https://apply.careers.microsoft.com/careers/job/{position_id}"
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            csrf = soup.find("meta", {"name": "_csrf"})
            if csrf and csrf.get("content"):
                return csrf["content"]
        except Exception as e:
            logger.warning(f"[microsoft] Failed to get CSRF for job {position_id}: {e}")
        return ""

    def fetch_job_detail(self, position_id: str) -> Optional[dict]:
        """
        Fetch detailed job information from API.
        
        Args:
            position_id: The job ID from the position listing
            
        Returns:
            Dict with job details or None if fetch failed
        """
        try:
            csrf_token = self._get_csrf_token(position_id)
            if not csrf_token:
                logger.warning(f"[microsoft] No CSRF token for job {position_id}")
                return None

            url = f"https://apply.careers.microsoft.com/careers/job/{position_id}"
            params = {
                "position_id": position_id,
                "domain": "microsoft.com",
                "hl": "en"
            }

            headers = {
                "Accept": "application/json",
                "Referer": url,
                "x-csrf-token": csrf_token,
                "x-browser-request-time": str(time.time())
            }

            response = self.session.get(
                DETAIL_API_URL,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()
            if data.get("status") == 200 and data.get("data"):
                return data["data"]

            return None

        except Exception as e:
            logger.warning(f"[microsoft] Error fetching detail for job {position_id}: {e}")
            return None

    @staticmethod
    def _fix_encoding(text: str) -> str:
        """Fix text encoding issues."""
        if not text:
            return ""
        text = text.replace("\u00a0", " ")
        text = text.replace("\u202f", " ")
        return text

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """Normalize whitespace in text."""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_link_text(tag) -> str:
        """Extract text from <a> tag, optionally with URL."""
        text = tag.get_text(" ", strip=True)
        href = tag.get("href")
        if href:
            return f"{text} ({href})"
        return text

    @classmethod
    def html_to_text(cls, html: str) -> str:
        """
        Convert HTML to plain text with proper formatting.
        
        Converts:
        - Lists to bullet points
        - Links to text with URLs
        - Headings to uppercase sections
        - Paragraphs to separated lines
        """
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")
        output = []

        def walk(node):
            for child in node.children:
                # Text nodes
                if isinstance(child, NavigableString):
                    text = str(child).strip()
                    if text:
                        output.append(text)
                    continue

                if not child.name:
                    continue

                # Line breaks
                if child.name == "br":
                    output.append("\n")
                    continue

                # Links
                if child.name == "a":
                    text = cls._extract_link_text(child)
                    if text:
                        output.append(text)
                    continue

                # Paragraphs / Divs
                if child.name in ["p", "div"]:
                    walk(child)
                    output.append("\n")
                    continue

                # Headings
                if child.name in ["b", "strong", "h1", "h2", "h3"]:
                    text = child.get_text(" ", strip=True)
                    if text:
                        output.append(f"\n{text.upper()}\n")
                    continue

                # Lists
                if child.name in ["ul", "ol"]:
                    for li in child.find_all("li", recursive=False):
                        text = li.get_text(" ", strip=True)
                        if text:
                            output.append(f"• {text}")
                    output.append("\n")
                    continue

                # Default: recurse
                walk(child)

        walk(soup)

        text = "\n".join(output)
        text = cls._fix_encoding(text)
        text = cls._normalize_whitespace(text)

        return text

    @classmethod
    def build_job_description(cls, job_data: dict) -> str:
        """Build formatted job description from job details."""
        sections = []

        job_desc = job_data.get("jobDescription", "")
        if job_desc:
            sections.append(cls.html_to_text(job_desc))

        return "\n\n".join(sections) if sections else ""
