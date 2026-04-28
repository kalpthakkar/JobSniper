"""
company/apple/apple.py — Apple Careers API adapter

Fetches job listings from Apple's careers API using pagination.
API supports 20 jobs per page with response containing totalRecords.

Response schema: 
  {
    "res": {
      "searchResults": [job_object, ...],
      "totalRecords": number
    }
  }
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from core.models import Job

logger = logging.getLogger("job_sniper.apple")

JOBS_PER_PAGE = 20


class AppleAdapter:
    """Adapter for fetching Apple Careers jobs from their API."""

    BASE_URL = "https://jobs.apple.com/api/v1/search"
    CSRF_URL = "https://jobs.apple.com/api/v1/CSRFToken"
    JOB_DETAIL_URL_TEMPLATE = "https://jobs.apple.com/en-us/details/"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        logger.info(f"[apple] Initializing AppleAdapter with timeout={timeout}s")
        self.session = requests.Session()
        # Set headers to mimic a browser
        self.session.headers.update({
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'Referer': 'https://jobs.apple.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        })
        self._csrf_token = None

    def _get_csrf_token(self) -> Optional[str]:
        """Fetch CSRF token from Apple's API."""
        try:
            logger.debug(f"[apple] Fetching CSRF token from {self.CSRF_URL}")
            response = self.session.get(self.CSRF_URL, timeout=self.timeout)
            response.raise_for_status()
            
            csrf_token = response.headers.get("x-apple-csrf-token")
            if csrf_token:
                logger.debug(f"[apple] CSRF token obtained: {csrf_token[:16]}…")
                self._csrf_token = csrf_token
                return csrf_token
            else:
                logger.warning("[apple] No CSRF token in response headers")
                return None
        except requests.Timeout as e:
            logger.error(f"[apple] TIMEOUT fetching CSRF token: {e}")
            return None
        except requests.RequestException as e:
            logger.warning(f"[apple] Failed to fetch CSRF token: {e}")
            return None

    def fetch_page(self, page: int, max_retries: int = 3) -> Optional[List[Dict]]:
        """
        Fetch jobs for a specific page with exponential backoff retry on rate limit.
        
        Args:
            page: Page number (1-based)
            max_retries: Max retry attempts on 429 rate limit
            
        Returns:
            List of job objects from searchResults, or None on error
        """
        # Ensure we have a valid CSRF token
        if not self._csrf_token:
            self._get_csrf_token()
        
        if not self._csrf_token:
            logger.warning("[apple] Cannot proceed without CSRF token")
            return None

        payload = {
            "query": "",
            "filters": {},
            "page": page,
            "locale": "en-us",
            "sort": "",
            "format": {
                "longDate": "MMMM D, YYYY",
                "mediumDate": "MMM D, YYYY"
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Origin": "https://jobs.apple.com",
            "Referer": "https://jobs.apple.com/search",
            "x-apple-csrf-token": self._csrf_token
        }

        for attempt in range(max_retries):
            try:
                logger.debug(f"[apple] HTTP POST {self.BASE_URL} page={page} (timeout={self.timeout}s)")
                response = self.session.post(
                    self.BASE_URL,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout
                )
                response.raise_for_status()
                
                data = response.json()
                search_results = data.get("res", {}).get("searchResults", [])
                logger.debug(f"[apple] HTTP 200 — got {len(search_results)} jobs from page {page}")
                return search_results
            except requests.exceptions.HTTPError as e:
                if response.status_code == 429:  # Too Many Requests
                    if attempt < max_retries - 1:
                        # Exponential backoff: 1s, 2s, 4s
                        wait_time = 2 ** attempt
                        logger.warning(f"[apple] Rate limited (429) on page {page}. Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"[apple] Rate limited (429) on page {page} after {max_retries} retries. Giving up.")
                        return None
                else:
                    logger.warning(f"[apple] HTTP {response.status_code} error on page {page}: {e}")
                    return None
            except requests.Timeout as e:
                logger.error(f"[apple] TIMEOUT on page {page} after {self.timeout}s: {e}")
                return None
            except requests.RequestException as e:
                logger.warning(f"[apple] Failed to fetch page {page}: {e}")
                return None
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"[apple] Failed to parse response from page {page}: {e}")
                return None
        
        return None

    def fetch_total_pages(self) -> Optional[int]:
        """
        Fetch first page to determine total number of pages.
        
        Returns:
            Total number of pages, or None on error
        """
        payload = {
            "query": "",
            "filters": {},
            "page": 1,
            "locale": "en-us",
            "sort": "",
            "format": {
                "longDate": "MMMM D, YYYY",
                "mediumDate": "MMM D, YYYY"
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Origin": "https://jobs.apple.com",
            "Referer": "https://jobs.apple.com/search",
            "x-apple-csrf-token": self._csrf_token or ""
        }

        try:
            if not self._csrf_token:
                self._get_csrf_token()
            
            if not self._csrf_token:
                logger.warning("[apple] Cannot fetch pages without CSRF token")
                return None

            response = self.session.post(
                self.BASE_URL,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            data = response.json()
            total_records = data.get("res", {}).get("totalRecords", 0)
            total_pages = (total_records + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE  # Ceiling division
            logger.debug(f"[apple] Total records: {total_records}, Total pages: {total_pages}")
            return total_pages
        except Exception as e:
            logger.warning(f"[apple] Failed to fetch total pages: {e}")
            return None

    def fetch_all_recent_jobs(self, hours: int = 6) -> Tuple[List[Dict], int]:
        """
        Fetch all jobs from the last N hours across all pages.
        
        Args:
            hours: Only include jobs posted within last N hours (default 6)
            
        Returns:
            Tuple of (filtered_jobs_list, total_records)
            where filtered_jobs_list contains only jobs posted within the time window
        """
        # Use timezone-aware UTC datetime to match parsed ISO 8601 datetimes
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        logger.info(f"[apple] Fetching jobs from last {hours} hours (cutoff: {cutoff_time.isoformat()})")
        
        # Get total pages
        total_pages = self.fetch_total_pages()
        if total_pages is None:
            logger.error("[apple] Failed to determine total pages")
            return [], 0
        
        logger.info(f"[apple] Total pages: {total_pages}")
        
        all_jobs = []
        total_records = 0
        jobs_outside_window = 0  # Track consecutive jobs outside time window
        
        for page in range(1, total_pages + 1):
            page_jobs = self.fetch_page(page)
            if page_jobs is None:
                logger.warning(f"[apple] Failed to fetch page {page}, stopping")
                break
            
            if not page_jobs:
                logger.debug(f"[apple] Page {page} is empty, stopping")
                break
            
            # Filter jobs by posting date (last 6 hours)
            page_has_recent = False
            for job in page_jobs:
                post_date_gmt = job.get("postDateInGMT")
                if post_date_gmt:
                    try:
                        # Parse ISO 8601 datetime
                        job_time = datetime.fromisoformat(post_date_gmt.replace('Z', '+00:00'))
                        if job_time >= cutoff_time:
                            all_jobs.append(job)
                            page_has_recent = True
                            jobs_outside_window = 0  # Reset counter
                            total_records = job.get("totalRecords", total_records)
                        else:
                            # Job is outside time window
                            jobs_outside_window += 1
                    except Exception as e:
                        logger.warning(f"[apple] Failed to parse date for job {job.get('id')}: {e}")
                        # Include job if we can't parse the date (conservative approach)
                        all_jobs.append(job)
                        page_has_recent = True
                        jobs_outside_window = 0
            
            logger.info(f"[apple] Page {page}/{total_pages}: fetched {len(page_jobs)} jobs, "
                       f"{len(all_jobs)} total in recent window")
            
            # OPTIMIZATION: If all jobs on this page are outside the time window,
            # and jobs appear to be sorted by date (likely), we can stop.
            # Check if we've seen 2+ consecutive pages with no recent jobs.
            if not page_has_recent and jobs_outside_window >= 20:
                logger.info(f"[apple] Page {page}: All {len(page_jobs)} jobs are outside time window. "
                           f"Stopping early (jobs likely sorted by date)")
                break
        
        
        logger.info(f"[apple] Fetched {len(all_jobs)} recent jobs from {total_records} total")
        return all_jobs, total_records

    @staticmethod
    def extract_location(job: Dict) -> str:
        """
        Extract location from job object.
        
        Builds location string as: "Name • Country" if countryName exists,
        otherwise just "Name"
        """
        locations = job.get("locations", [])
        location_parts = []
        
        for loc in locations:
            name = loc.get("name", "")
            country = loc.get("countryName", "")
            
            if name and country:
                location_parts.append(f"{name} • {country}")
            elif name:
                location_parts.append(name)
        
        return ", ".join(location_parts)

    @staticmethod
    def is_remote(job: Dict) -> bool:
        """Check if job is remote."""
        is_remote = job.get("homeOffice", False)
        if is_remote:
            return True
        
        # Check if 'remote' is in location string
        location = AppleAdapter.extract_location(job)
        return 'remote' in location.lower()


class AppleJobDetailFetcher:
    """Fetches detailed job descriptions from Apple job detail page."""
    
    BASE_URL = "https://jobs.apple.com/en-us/details/"
    
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        })
    
    def fetch_job_detail(self, position_id: str) -> Optional[Dict]:
        """
        Fetch job detail page and extract hydration data.
        
        Args:
            position_id: Job position ID
            
        Returns:
            Parsed job data dict, or None on error
        """
        url = f"{self.BASE_URL}{position_id}"
        
        try:
            logger.debug(f"[apple] Fetching job details from {url}")
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Find the script containing hydration data
            script_tag = None
            for script in soup.find_all("script"):
                if script.string and "window.__staticRouterHydrationData" in script.string:
                    script_tag = script.string
                    break
            
            if not script_tag:
                logger.warning(f"[apple] Hydration data not found for job {position_id}")
                return None
            
            # Extract JSON string
            match = re.search(
                r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*)"\);',
                script_tag
            )
            
            if not match:
                logger.warning(f"[apple] Could not extract JSON for job {position_id}")
                return None
            
            raw_json = match.group(1)
            
            # Unescape the string
            clean_json = raw_json.encode("utf-8").decode("unicode_escape")
            
            # Load JSON
            data = json.loads(clean_json)
            
            # Navigate to job details
            job_data = self._find_job_details(data)
            if job_data and 'jobsData' in job_data:
                logger.debug(f"[apple] Successfully extracted job details for {position_id}")
                return job_data['jobsData']
            else:
                logger.warning(f"[apple] Job data structure not found for {position_id}")
                return None
        except requests.Timeout as e:
            logger.error(f"[apple] TIMEOUT fetching job details for {position_id}: {e}")
            return None
        except requests.RequestException as e:
            logger.warning(f"[apple] Failed to fetch job details for {position_id}: {e}")
            return None
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[apple] Failed to parse job details for {position_id}: {e}")
            return None
    
    @staticmethod
    def _find_job_details(obj) -> Optional[Dict]:
        """Recursively find job details object in hydration data."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower().startswith("job") and isinstance(v, dict):
                    return v
                result = AppleJobDetailFetcher._find_job_details(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = AppleJobDetailFetcher._find_job_details(item)
                if result:
                    return result
        return None
    
    @staticmethod
    def _html_to_bullets(html_text: str) -> str:
        """Convert HTML to bullet point format."""
        if not html_text:
            return ""
        
        soup = BeautifulSoup(html_text, "html.parser")
        bullets = []
        
        # Case 1: Proper <li> lists
        for li in soup.find_all("li"):
            text = li.get_text(strip=True)
            if text:
                bullets.append(f"• {text}")
        
        # Case 2: No <li>, fallback to line breaks
        if not bullets:
            text = soup.get_text("\n")
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            bullets = [f"• {line}" for line in lines]
        
        return "\n".join(bullets)
    
    @staticmethod
    def build_job_description(job_data: Dict) -> Optional[str]:
        """Build complete job description from job data."""
        parts = []
        
        if job_data.get("jobSummary"):
            parts.append(f"Summary:\n{job_data['jobSummary'].strip()}")
        
        if job_data.get("description"):
            parts.append(f"Description:\n{job_data['description'].strip()}")
        
        if job_data.get("responsibilities"):
            parts.append(f"Responsibilities:\n{AppleJobDetailFetcher._html_to_bullets(job_data['responsibilities'])}")
        
        if job_data.get("preferredQualifications"):
            parts.append(f"Preferred Qualifications:\n{AppleJobDetailFetcher._html_to_bullets(job_data['preferredQualifications'])}")
        
        if job_data.get("minimumQualifications"):
            parts.append(f"Minimum Requirements:\n{AppleJobDetailFetcher._html_to_bullets(job_data['minimumQualifications'])}")
        
        return "\n\n".join(parts) if parts else None
