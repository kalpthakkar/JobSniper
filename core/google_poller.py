"""
core/google_poller.py — Dedicated poller for Google Careers with pagination.

This poller runs independently, fetching jobs from Google Careers pages
starting from page 1, incrementing until no jobs are found, then waiting
for a cooldown period before restarting the cycle.
"""
import json
import logging
import math
import requests
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from core.database import JobDatabase
from core.models import Job
from notifications.notifier import Notifier
from company.google.google import GoogleAdapter
from company.google.google_job_detail import GoogleJobDetailFetcher

logger = logging.getLogger("job_sniper.google_poller")


class GooglePoller:
    def __init__(
        self,
        db: JobDatabase,
        notifier: Notifier,
        cooldown_minutes: int = 3,
        request_timeout: int = 10,
    ):
        self.db = db
        self.notifier = notifier
        self.cooldown_seconds = cooldown_minutes * 60
        self.adapter = GoogleAdapter(timeout=request_timeout)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self):
        logger.info("🚀 Starting Google Careers poller")
        logger.info("[google] Initializing GoogleJobDetailFetcher with timeout=30s")
        logger.info("[google] GooglePoller fully initialized")
        self._thread.start()

    def stop(self):
        logger.info("⏹ Stopping Google Careers poller…")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._poll_cycle()
            except Exception as e:
                logger.error(f"[google] Polling cycle error: {e}")
                time.sleep(60)  # Wait a minute before retrying on error

            if not self._stop.is_set():
                logger.info(f"[google] Cycle complete, cooling down for {self.cooldown_seconds}s")
                self._stop.wait(self.cooldown_seconds)


    def _assign_page_ranges(self, total_pages: int, num_workers: int) -> List[Tuple[int, int]]:
        """Distribute pages evenly across workers.
        
        Args:
            total_pages: Total number of pages to fetch
            num_workers: Number of worker threads
            
        Returns:
            List of (start_page, end_page) tuples for each worker
        """
        ranges = []
        base = total_pages // num_workers
        remainder = total_pages % num_workers
        
        start = 1
        for i in range(num_workers):
            # Give one extra page to first `remainder` workers
            pages_for_this_worker = base + (1 if i < remainder else 0)
            end = start + pages_for_this_worker - 1
            ranges.append((start, end))
            start = end + 1
        
        return ranges

    def _fetch_pages_worker(self, worker_id: int, start_page: int, end_page: int) -> Dict[int, List[Dict]]:
        """Worker function: fetch pages in range [start_page, end_page] inclusive.
        
        Creates per-worker session to avoid connection pooling issues.
        Handles errors gracefully and continues with remaining pages.
        
        Args:
            worker_id: Worker identifier for logging
            start_page: First page to fetch (inclusive)
            end_page: Last page to fetch (inclusive)
            
        Returns:
            Dict mapping page number to list of jobs
        """
        # Create per-worker session for parallel HTTP requests
        worker_session = requests.Session()
        worker_session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'max-age=0',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        })
        
        page_jobs = {}  # {page_num: [jobs]}
        successful_pages = 0
        failed_pages = 0
        
        try:
            for page in range(start_page, end_page + 1):
                if self._stop.is_set():
                    logger.debug(f"[google] Worker {worker_id} stop signal received")
                    break
                
                logger.debug(f"[google] Worker {worker_id} fetching page {page}...")
                
                try:
                    jobs_raw = self.adapter.fetch_jobs_page(page, session=worker_session)
                    if jobs_raw:
                        page_jobs[page] = jobs_raw
                        successful_pages += 1
                        logger.debug(f"[google] Worker {worker_id} page {page}: {len(jobs_raw)} jobs")
                    else:
                        logger.debug(f"[google] Worker {worker_id} page {page}: empty")
                        failed_pages += 1
                except Exception as e:
                    logger.warning(f"[google] Worker {worker_id} page {page}: {e}")
                    failed_pages += 1
                    continue
                
                # Small delay between pages for politeness
                time.sleep(0.5)
            
            logger.info(
                f"[google] Worker {worker_id}: pages {start_page}-{end_page}, "
                f"success={successful_pages}, failed={failed_pages}"
            )
        except Exception as e:
            logger.error(f"[google] Worker {worker_id} fatal error: {e}")
        finally:
            worker_session.close()
        
        return page_jobs

    def _fetch_job_details(self, new_jobs: List[Job]) -> None:
        """Fetch full job details for newly discovered jobs.
        
        Populates job.description field by merging qualifications, about_the_job,
        and responsibilities from the job detail page.
        
        Args:
            new_jobs: List of newly discovered Job objects to enrich with details
        """
        fetcher = GoogleJobDetailFetcher(timeout=self.adapter.timeout)
        details_fetched = 0
        details_failed = 0
        
        for job in new_jobs:
            if self._stop.is_set():
                logger.debug("[google] Stop signal received during detail fetching")
                break
            
            try:
                logger.debug(f"[google] Fetching job details for {job.id}...")
                detail = fetcher.fetch_job_detail(job.url)
                
                if detail:
                    # Merge all three sections into single description
                    parts = []
                    if detail.qualifications:
                        parts.append(detail.qualifications)
                    if detail.about_the_job:
                        parts.append(detail.about_the_job)
                    if detail.responsibilities:
                        parts.append(detail.responsibilities)
                    
                    job.description = "\n\n".join(parts) if parts else None
                    if job.description:
                        details_fetched += 1
                        logger.debug(f"[google] Job {job.id}: {len(job.description)} char description")
                    else:
                        details_failed += 1
                else:
                    logger.warning(f"[google] Could not fetch details for job {job.id}")
                    job.description = None
                    details_failed += 1
                    
            except Exception as e:
                logger.error(f"[google] Error fetching details for job {job.id}: {e}")
                job.description = None
                details_failed += 1
        
        logger.info(
            f"[google] Detail fetching: {details_fetched} success, {details_failed} failed "
            f"(out of {len(new_jobs)} new jobs)"
        )

    def _poll_cycle(self):
        """Poll all pages using parallel workers for maximum throughput.
        
        Strategy:
        1. Fetch page 1 to extract pagination info (total pages)
        2. Distribute pages across NUM_WORKERS workers
        3. Launch ThreadPoolExecutor to fetch pages in parallel
        4. Aggregate results and deduplicate
        5. Process combined results
        """
        logger.info("[google] Starting poll cycle...")
        logger.info("[google] Beginning page fetch loop...")
        
        NUM_WORKERS = 5
        
        # Step 1: Fetch page 1 to get pagination info
        logger.info("[google] Fetching page 1 for pagination info...")
        page1_html = self.adapter.fetch_page_html(1)
        if not page1_html:
            logger.warning("[google] Failed to fetch page 1, cycle aborted")
            return
        
        page1_jobs = self.adapter.extract_jobs(page1_html)
        logger.info(f"[google] Page 1 returned {len(page1_jobs)} job(s)")
        
        # Extract pagination info
        pagination_info = self.adapter.extract_pagination_info(page1_html)
        if not pagination_info:
            # Fallback: just use page 1 if we can't determine total
            logger.warning("[google] Could not extract pagination info, processing only page 1")
            all_jobs = page1_jobs
        else:
            jobs_per_page, total_jobs = pagination_info
            total_pages = math.ceil(total_jobs / jobs_per_page)
            logger.info(f"[google] Total pages: {total_pages}")
            
            # Step 2: Assign page ranges
            page_ranges = self._assign_page_ranges(total_pages, NUM_WORKERS)
            logger.info(f"[google] Assigning {total_pages} pages to {NUM_WORKERS} workers")
            for i, (start, end) in enumerate(page_ranges):
                logger.debug(f"[google] Worker {i+1}: pages {start}-{end}")
            
            # Step 3: Parallel fetch using ThreadPoolExecutor
            all_page_jobs = {}
            failed_workers = []
            
            with ThreadPoolExecutor(max_workers=NUM_WORKERS, thread_name_prefix="google_worker") as executor:
                # Submit all workers
                futures = {}
                for worker_id, (start_page, end_page) in enumerate(page_ranges, start=1):
                    future = executor.submit(
                        self._fetch_pages_worker,
                        worker_id,
                        start_page,
                        end_page
                    )
                    futures[future] = worker_id
                
                # Collect results as they complete
                completed_workers = 0
                for future in as_completed(futures):
                    worker_id = futures[future]
                    try:
                        page_jobs = future.result(timeout=300)  # 5 min timeout per worker
                        all_page_jobs.update(page_jobs)
                        completed_workers += 1
                    except Exception as e:
                        logger.error(f"[google] Worker {worker_id} exception: {e}")
                        failed_workers.append(worker_id)
                
                logger.info(
                    f"[google] Parallel fetch complete: "
                    f"{completed_workers}/{NUM_WORKERS} workers, "
                    f"{len(all_page_jobs)} pages"
                )
                if failed_workers:
                    logger.warning(f"[google] Failed workers: {failed_workers}")
            
            # Step 4: Combine and deduplicate
            job_ids_seen = set()
            all_jobs = []
            
            # Add page 1 jobs first
            for job in page1_jobs:
                job_id = job.get("id")
                if job_id and job_id not in job_ids_seen:
                    all_jobs.append(job)
                    job_ids_seen.add(job_id)
            
            # Add jobs from other pages (skip page 1 since we already processed it)
            for page_num in sorted(all_page_jobs.keys()):
                if page_num == 1:
                    continue  # Skip page 1, already added above
                for job in all_page_jobs[page_num]:
                    job_id = job.get("id")
                    if job_id and job_id not in job_ids_seen:
                        all_jobs.append(job)
                        job_ids_seen.add(job_id)
            
            logger.info(
                f"[google] Deduplication: "
                f"{len(all_jobs)} unique jobs from {len(all_page_jobs)} pages"
            )
        
        # Normalize jobs
        normalized_jobs = [self.adapter.normalize_job(job) for job in all_jobs]
        logger.info(f"[google] Cycle complete: processed {len(normalized_jobs)} jobs")
        
        # Process all jobs at once
        if normalized_jobs:
            self._process_jobs(normalized_jobs)


    def _process_jobs(self, jobs: List):
        """Process a batch of jobs: check for new ones and notify."""
        endpoint = "google_careers"  # Use a fixed endpoint for Google

        # Get current seen IDs
        record = self.db.get_record(endpoint, "ats")
        seen_ids = record["seen_ids"] if record else []
        all_ids = [job.id for job in jobs]
        
        # Debug: check for duplicate IDs in current fetch
        unique_ids = set(all_ids)
        if len(unique_ids) != len(all_ids):
            duplicate_count = len(all_ids) - len(unique_ids)
            logger.warning(f"[google] ⚠️  Found {duplicate_count} duplicate job IDs in current fetch!")
            # Use only unique IDs going forward
            all_ids = sorted(unique_ids)
            jobs = [job for job in jobs if job.id in unique_ids]  # Keep only one of each ID

        # First ever run → seed baseline silently, no alert
        if record is None:
            canonical = json.dumps(sorted(all_ids))
            new_hash = JobDatabase.compute_hash(canonical)
            self.db.update(endpoint, "ats", new_hash, all_ids)
            logger.info(f"[google] 🌱 Baseline set — {len(all_ids)} job(s) recorded. Monitoring started.")
            return

        # Check for changes
        if set(seen_ids) == set(all_ids):
            # No changes in job IDs
            logger.debug(f"[google] ✓ Stable job set: {len(all_ids)} jobs")
            return

        truly_new_ids = [jid for jid in all_ids if jid not in seen_ids]
        removed_ids = [jid for jid in seen_ids if jid not in all_ids]
        
        # Debug log: show counts and hash consistency
        logger.debug(
            f"[google] Job set comparison: "
            f"total={len(all_ids)}, seen={len(seen_ids)}, "
            f"new={len(truly_new_ids)}, removed={len(removed_ids)}"
        )

        # Notify about new jobs
        if truly_new_ids:
            new_jobs = [job for job in jobs if job.id in truly_new_ids]
            
            # Handle large sudden job drops/additions (likely from interruption or staleness)
            if len(new_jobs) >= 300:
                logger.warning(
                    f"[google] 🔄 {len(new_jobs)} job(s) recovered/discovered (likely from interruption or stale data). "
                    f"Silently adding to baseline without notification."
                )
            else:
                logger.info(f"[google] 🚨 {len(new_jobs)} NEW job(s)!")
                # Fetch job details for notification
                logger.info(f"[google] Fetching job details for {len(new_jobs)} new job(s)...")
                self._fetch_job_details(new_jobs)
                self.notifier.notify(new_jobs)

        if removed_ids:
            logger.info(f"[google] ➖ {len(removed_ids)} job(s) removed")

        # Update database with only current jobs (not a merge with old seen_ids)
        # This ensures removed jobs are actually deleted from tracking
        canonical = json.dumps(sorted(all_ids))
        new_hash = JobDatabase.compute_hash(canonical)
        self.db.update(endpoint, "ats", new_hash, all_ids)