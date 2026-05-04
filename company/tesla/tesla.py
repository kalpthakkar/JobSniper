"""
company/tesla/tesla.py — Tesla Careers adapter using Playwright browser automation.

Fetches jobs from Tesla careers page via JavaScript API endpoints.
Uses persistent browser context with optimal mode selection for Akamai avoidance.
"""
import logging
import re
import time
from enum import Enum
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright

logger = logging.getLogger("job_sniper.tesla")

PROFILE_DIR = "./tesla_profile"


class BrowserMode(Enum):
    """Browser launch modes for Tesla career page."""
    HEADLESS = "headless"      # Priority 1: headless + minimized (most Akamai-friendly)
    HEADED = "headed"          # Priority 2: minimized without headless


class TeslaAdapter:
    """
    Adapter for fetching jobs from Tesla careers website using Playwright.
    
    Strategy:
    - Uses persistent browser context to maintain session
    - Tries headless mode first (most Akamai-friendly)
    - Falls back to headed mode if needed
    - Applies human-like interactions before data fetch
    """

    def __init__(self, timeout: int = 60, user_data_dir: str = PROFILE_DIR):
        self.timeout = timeout
        self.user_data_dir = user_data_dir

    def _get_browser_args(self, mode: BrowserMode) -> tuple[bool, List[str]]:
        """Get browser launch arguments based on mode."""
        base_args = ["--disable-blink-features=AutomationControlled"]

        if mode == BrowserMode.HEADLESS:
            # Priority 1: Headless + minimized
            return True, base_args + [
                "--headless=new",
                "--start-minimized",
                "--window-position=-2000,-2000",
                "--window-size=800,600",
            ]
        else:  # BrowserMode.HEADED
            # Priority 2: Minimized without headless
            return False, base_args + [
                "--start-minimized",
                "--window-position=-2000,-2000",
                "--window-size=800,600",
            ]

    def _fetch_data(self, url: str, request_query: str, mode: BrowserMode) -> Optional[Dict]:
        """
        Internal method to fetch data from Tesla using specified mode.
        
        Args:
            url: URL to navigate to
            request_query: JavaScript async function to execute
            mode: Browser mode to use
            
        Returns:
            Parsed JSON response or None
        """
        headless, args = self._get_browser_args(mode)
        mode_name = mode.value

        logger.debug(f"[Tesla] Attempting fetch ({mode_name})")

        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=headless,
                    viewport={"width": 800, "height": 600},
                    args=args,
                    timeout=self.timeout * 1000,
                )

                page = context.pages[0] if context.pages else context.new_page()

                # Navigate to page
                page.goto(url, wait_until="domcontentloaded", timeout=90000)

                # Human-like behavior (optional but helps with detection)
                page.mouse.move(100, 200)
                page.wait_for_timeout(2000)
                page.mouse.wheel(0, 500)
                page.wait_for_timeout(2000)

                # Retry fetch until data is ready
                data = None
                for attempt in range(5):
                    try:
                        data = page.evaluate(request_query)
                        if data and data.get("listings"):
                            logger.debug(
                                f"[Tesla] Successfully fetched {len(data.get('listings', []))} jobs ({mode_name})"
                            )
                            break
                    except Exception as e:
                        logger.debug(f"[Tesla] Fetch attempt {attempt + 1}/5 ({mode_name}): {e}")

                    if attempt < 4:
                        time.sleep(2)

                context.close()
                return data

        except Exception as e:
            logger.warning(f"[Tesla] {mode_name} mode failed: {e}")
            return None

    def fetch_all_jobs(self) -> List[Dict]:
        """
        Fetch all job listings from Tesla careers.
        
        Tries headless mode first, falls back to headed mode.
        Returns list of {id, apply_url} dicts.
        """
        request_query = """
            async () => {
                const res = await fetch('/cua-api/apps/careers/state', {
                    method: 'GET',
                    credentials: 'include'
                });
                return await res.json();
            }
        """

        # Try modes in priority order
        for mode in [BrowserMode.HEADLESS, BrowserMode.HEADED]:
            data = self._fetch_data(
                "https://www.tesla.com/careers",
                request_query,
                mode
            )

            if data and data.get("listings"):
                return self._normalize_jobs(data.get("listings", []))

            time.sleep(1)

        logger.warning("[Tesla] All fetch modes exhausted, no jobs retrieved")
        return []

    def fetch_job_details(self, job_url: str) -> Optional[Dict]:
        """
        Fetch detailed information for a specific job.
        Called only when notifying about a new job.
        
        Args:
            job_url: Tesla job URL (e.g., https://www.tesla.com/careers/search/job/title--12345)
            
        Returns:
            Job details dict or None on failure
        """
        return self.fetch_job_details_batch([job_url]).get(job_url)

    def _fetch_job_details_batch_mode(
        self, job_urls: List[str], mode: BrowserMode
    ) -> Dict[str, Optional[Dict]]:
        """Fetch detailed information for multiple jobs using a specific browser mode."""
        results = {job_url: None for job_url in job_urls}
        headless, args = self._get_browser_args(mode)

        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=headless,
                    viewport={"width": 800, "height": 600},
                    args=args,
                    timeout=self.timeout * 1000,
                )

                for job_url in job_urls:
                    try:
                        job_id = self._extract_job_id(job_url)
                        page = context.new_page()
                        logger.debug(f"[Tesla] Navigating to job page: {job_url}")
                        page.goto(job_url, wait_until="domcontentloaded", timeout=90000)

                        page.mouse.move(100, 200)
                        page.wait_for_timeout(2000)
                        page.mouse.wheel(0, 500)
                        page.wait_for_timeout(2000)

                        for attempt in range(5):
                            try:
                                request_query = f"""
                                    async () => {{
                                        const res = await fetch('/cua-api/careers/job/{job_id}', {{
                                            method: 'GET',
                                            credentials: 'include'
                                        }});
                                        return await res.json();
                                    }}
                                """
                                data = page.evaluate(request_query)
                                if data and data.get("id"):
                                    logger.debug(f"[Tesla] Fetched details for job {job_id} ({mode.value})")
                                    results[job_url] = data
                                    break
                                logger.debug(
                                    f"[Tesla] Batch fetch response missing 'id' for job {job_id} ({mode.value}); attempt {attempt+1}"
                                )
                            except Exception as e:
                                logger.debug(
                                    f"[Tesla] Batch fetch attempt {attempt + 1}/5 for job {job_id} ({mode.value}): {e}"
                                )

                            if attempt < 4:
                                time.sleep(2)

                        page.close()
                    except Exception as e:
                        logger.warning(f"[Tesla] Failed to fetch details for job URL {job_url} ({mode.value}): {e}")

                context.close()
                return results

        except Exception as e:
            logger.warning(f"[Tesla] Batch context initialization failed ({mode.value}): {e}")
            return results

    def fetch_job_details_batch(self, job_urls: List[str], max_retries: int = 2) -> Dict[str, Optional[Dict]]:
        """
        Fetch detailed information for multiple jobs in a single browser context.
        
        Retries across browser modes with exponential backoff.
        
        Args:
            job_urls: List of job URLs to fetch
            max_retries: Number of retry attempts across modes
            
        Returns:
            Dict mapping job_url -> job details (or None if not fetched)
        """
        if not job_urls:
            return {}

        for retry in range(max_retries):
            for mode in [BrowserMode.HEADLESS, BrowserMode.HEADED]:
                results = self._fetch_job_details_batch_mode(job_urls, mode)
                successful_count = sum(1 for v in results.values() if v is not None)
                
                if successful_count > 0:
                    logger.info(f"[Tesla] Successfully fetched {successful_count}/{len(job_urls)} job details ({mode.value})")
                    return results
                
                logger.warning(f"[Tesla] No valid job detail payloads in {mode.value} mode (attempt {retry + 1}/{max_retries})")
            
            # Wait before retry
            if retry < max_retries - 1:
                wait_time = 2 ** (retry + 1)  # Exponential backoff: 2s, 4s
                logger.debug(f"[Tesla] Waiting {wait_time}s before retry...")
                time.sleep(wait_time)

        # All retries exhausted
        logger.warning(f"[Tesla] Failed to fetch job details after {max_retries} retry attempt(s) across all modes")
        return results
    @staticmethod
    def _normalize_jobs(jobs_list: List[Dict]) -> List[Dict]:
        """Normalize job list to standard format with IDs and apply URLs."""
        return [
            TeslaAdapter._normalize_job(job)
            for job in jobs_list
            if job.get("id")
        ]

    @staticmethod
    def _normalize_job(job: Dict) -> Dict:
        """
        Normalize a single job to {id, apply_url} format.
        
        Args:
            job: Raw job dict from Tesla API with 'id' and 't' (title)
            
        Returns:
            Normalized job with 'id' and 'apply_url'
        """
        job_id = job.get("id", "")
        job_title = job.get("t", "")
        apply_url = TeslaAdapter._build_apply_url(job_title, job_id)
        return {"id": str(job_id), "apply_url": apply_url}

    @staticmethod
    def _build_apply_url(job_title: str, job_id: int | str) -> str:
        """
        Build Tesla job URL from title and ID.
        
        Converts title to lowercase, removes special chars, replaces spaces with dashes.
        """
        if not isinstance(job_title, str):
            return f"https://www.tesla.com/careers/search/job/job--{job_id}"

        # Convert to lowercase
        result = job_title.lower()

        # Remove special characters (keep letters, numbers, and spaces)
        result = re.sub(r"[^a-z0-9\s]", "", result)

        # Trim leading and trailing spaces
        result = result.strip()

        # Replace spaces with dashes
        result = re.sub(r"\s+", "-", result)

        return f"https://www.tesla.com/careers/search/job/{result}--{job_id}"

    @staticmethod
    def _extract_job_id(job_url: str) -> str:
        """
        Extract job ID from Tesla job URL.
        
        Args:
            job_url: URL like https://www.tesla.com/careers/search/job/title--12345
            
        Returns:
            Job ID string
            
        Raises:
            ValueError: If URL format is invalid
        """
        match = re.search(r"-(\d+)$", job_url)
        if not match:
            raise ValueError(f"Invalid Tesla job URL: {job_url}")
        return match.group(1)

