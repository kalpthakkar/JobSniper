"""
core/google_poller.py — Dedicated poller for Google Careers with pagination.

This poller runs independently, fetching jobs from Google Careers pages
starting from page 1, incrementing until no jobs are found, then waiting
for a cooldown period before restarting the cycle.
"""
import json
import logging
import threading
import time
from typing import List

from core.database import JobDatabase
from notifications.notifier import Notifier
from company.google.google import GoogleAdapter

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

    def _poll_cycle(self):
        """Poll all pages starting from 1 until no jobs found."""
        all_jobs = []
        page = 1

        while not self._stop.is_set():
            logger.debug(f"[google] Polling page {page}")

            jobs_raw = self.adapter.fetch_jobs_page(page)
            if not jobs_raw:
                logger.debug(f"[google] No jobs found on page {page}, ending cycle")
                break

            # Normalize jobs
            jobs = [self.adapter.normalize_job(job) for job in jobs_raw]
            all_jobs.extend(jobs)

            page += 1

            # Small delay between pages to be respectful
            time.sleep(1)

        logger.info(f"[google] Cycle complete: processed {len(all_jobs)} jobs across {page-1} pages")

        # Process all jobs at once
        if all_jobs:
            self._process_jobs(all_jobs)

    def _process_jobs(self, jobs: List):
        """Process a batch of jobs: check for new ones and notify."""
        endpoint = "google_careers"  # Use a fixed endpoint for Google

        # Get current seen IDs
        record = self.db.get_record(endpoint, "ats")
        seen_ids = record["seen_ids"] if record else []
        all_ids = [job.id for job in jobs]

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
            logger.debug(f"[google] ✓ Stable job set")
            return

        truly_new_ids = [jid for jid in all_ids if jid not in seen_ids]
        removed_ids = [jid for jid in seen_ids if jid not in all_ids]

        # Notify about new jobs
        if truly_new_ids:
            new_jobs = [job for job in jobs if job.id in truly_new_ids]
            logger.info(f"[google] 🚨 {len(new_jobs)} NEW job(s)!")
            self.notifier.notify(new_jobs)

        if removed_ids:
            logger.info(f"[google] ➖ {len(removed_ids)} job(s) removed")

        # Update database with only current jobs (not a merge with old seen_ids)
        # This ensures removed jobs are actually deleted from tracking
        canonical = json.dumps(sorted(all_ids))
        new_hash = JobDatabase.compute_hash(canonical)
        self.db.update(endpoint, "ats", new_hash, all_ids)