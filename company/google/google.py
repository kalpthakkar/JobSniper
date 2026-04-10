"""
company/google/google.py — Google Careers job scraper adapter.

This adapter fetches job listings from Google's careers page by scraping HTML
with pagination. It extracts job IDs, titles, locations, and other metadata.
"""
import json
import logging
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from core.models import Job

logger = logging.getLogger("job_sniper.company.google")


class GoogleAdapter:
    """Adapter for scraping Google Careers jobs."""

    BASE_URL = "https://www.google.com/about/careers/applications/jobs/results"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
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

    def fetch_page_html(self, page: int) -> Optional[str]:
        """Fetch HTML content for a specific page number."""
        url = f"{self.BASE_URL}?page={page}"
        try:
            logger.debug(f"[google] Fetching page {page}: {url}")
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
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

    def fetch_jobs_page(self, page: int) -> List[Dict]:
        """Fetch and parse jobs from a specific page."""
        html = self.fetch_page_html(page)
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