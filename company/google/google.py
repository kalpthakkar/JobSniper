"""
company/google/google.py — Google Careers job scraper adapter.

This adapter fetches job listings from Google's careers page by scraping HTML
with pagination. It extracts job IDs, titles, locations, and other metadata.
"""
import json
import logging
import math
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from core.models import Job

logger = logging.getLogger("job_sniper.company.google")


class GoogleAdapter:
    """Adapter for scraping Google Careers jobs."""

    BASE_URL = "https://www.google.com/about/careers/applications/jobs/results"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        logger.info(f"[google] Initializing GoogleAdapter with timeout={timeout}s")
        self.session = requests.Session()
        # Set headers to mimic a browser
        self.session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        })

    def fetch_page_html(self, page: int, session: Optional[requests.Session] = None) -> Optional[str]:
        """Fetch HTML content for a specific page number.
        
        Args:
            page: Page number to fetch
            session: Optional session object. If None, uses self.session.
        """
        url = f"{self.BASE_URL}?page={page}"
        fetch_session = session if session is not None else self.session
        try:
            logger.debug(f"[google] HTTP GET {url} (timeout={self.timeout}s)")
            response = fetch_session.get(url, timeout=self.timeout)
            response.raise_for_status()
            logger.debug(f"[google] HTTP 200 — got {len(response.text)} bytes from page {page}")
            return response.text
        except requests.Timeout as e:
            logger.error(f"[google] TIMEOUT on page {page} after {self.timeout}s: {e}")
            return None
        except requests.RequestException as e:
            logger.warning(f"[google] Failed to fetch page {page}: {e}")
            return None

    def extract_job_ids(self, html: str) -> List[str]:
        """Extract job IDs from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        ids = []
        for li in soup.select("ul.spHGqe > li"):
            ssk = li.get("ssk")
            if ssk and ":" in ssk:
                ids.append(ssk.split(":", 1)[1])
        return ids

    def extract_jobs(self, html: str) -> List[Dict]:
        """Extract full job details from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        for li in soup.select("ul.spHGqe > li"):
            job = {}

            # ID
            ssk = li.get("ssk")
            if ssk and ":" in ssk:
                job["id"] = ssk.split(":", 1)[1]

            # Title
            title = li.select_one("h3.QJPWVe")
            job["title"] = title.get_text(strip=True) if title else None

            # Company (always Google)
            job["company"] = "Google"

            # Location
            location = li.select_one(".r0wTof")
            job["location"] = location.get_text(strip=True) if location else None

            # Experience
            exp = li.select_one(".wVSTAb")
            job["experience"] = exp.get_text(strip=True) if exp else None

            # Link
            link = li.select_one("a[href]")
            if link and link.get("href"):
                job["link"] = 'https://www.google.com/about/careers/applications/' + link["href"]

            # Qualifications
            quals = li.select(".Xsxa1e ul li")
            job["qualifications"] = [q.get_text(strip=True) for q in quals]

            if job.get("id"):  # Only add if we have an ID
                jobs.append(job)

        return jobs

    def extract_pagination_info(self, html: str) -> Optional[Tuple[int, int]]:
        """Extract total jobs and pages from pagination div.
        
        Returns: (jobs_per_page, total_jobs) or None if not found
        """
        soup = BeautifulSoup(html, "html.parser")
        pagination_div = soup.select_one('div[jsname="uEp2ad"]')
        
        if not pagination_div:
            logger.warning("[google] Could not find pagination div")
            return None
        
        pagination_text = pagination_div.get_text(strip=True)
        logger.debug(f"[google] Pagination text: {pagination_text}")
        
        try:
            parts = pagination_text.split(' ')
            if len(parts) < 3:
                return None
            
            range_part = parts[0]
            if '‑' in range_part:
                jobs_per_page = int(range_part.split('‑')[1])
            elif '-' in range_part:
                jobs_per_page = int(range_part.split('-')[1])
            else:
                return None
            
            total_jobs = int(parts[-1])
            logger.info(f"[google] Pagination: {jobs_per_page} jobs/page, {total_jobs} total")
            return (jobs_per_page, total_jobs)
        except (ValueError, IndexError) as e:
            logger.warning(f"[google] Error parsing pagination: {e}")
            return None

    def fetch_jobs_page(self, page: int, session: Optional[requests.Session] = None) -> List[Dict]:
        """Fetch and parse jobs from a specific page.
        
        Args:
            page: Page number to fetch
            session: Optional session object for parallel worker use
        """
        html = self.fetch_page_html(page, session=session)
        if not html:
            return []
        return self.extract_jobs(html)

    def normalize_job(self, raw_job: Dict) -> Job:
        """Convert raw job dict to Job model."""
        return Job(
            id=raw_job["id"],
            title=raw_job.get("title", ""),
            company=raw_job.get("company", "Google"),
            location=raw_job.get("location", ""),
            department="",  # Google doesn't provide department in this view
            url=raw_job.get("link", ""),
            posted_at=None,  # Not available in HTML
            remote=False,  # Not specified
            salary=None,  # Not available
            raw=raw_job,
        )