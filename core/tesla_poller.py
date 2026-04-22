"""
core/tesla_poller.py — Dedicated poller for Tesla Careers with pagination and disappearance tracking.

This poller runs independently, fetching jobs from Tesla careers page
at a configured cooldown interval, tracking job IDs, and notifying on new postings.
Uses disappearance counters to handle API inconsistencies.
"""
import json
import logging
import threading
import time
from typing import List, Optional

from core.database import JobDatabase
from core.models import Job
from notifications.notifier import Notifier
from company.tesla.tesla import TeslaAdapter

logger = logging.getLogger("job_sniper.tesla_poller")

TESLA_DISAPPEARANCE_THRESHOLD = 3  # Remove job if missing from 3 consecutive polls


class TeslaPoller:
    def __init__(
        self,
        db: JobDatabase,
        notifier: Notifier,
        cooldown_minutes: int = 3,
        request_timeout: int = 60,
    ):
        self.db = db
        self.notifier = notifier
        self.cooldown_seconds = cooldown_minutes * 60
        self.adapter = TeslaAdapter(timeout=request_timeout)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self):
        logger.info("🚀 Starting Tesla Careers poller")
        self._thread.start()

    def stop(self):
        logger.info("⏹ Stopping Tesla Careers poller…")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._poll_cycle()
            except Exception as e:
                logger.error(f"[Tesla] Polling cycle error: {e}", exc_info=True)
                time.sleep(60)  # Wait a minute before retrying on error

            if not self._stop.is_set():
                logger.info(
                    f"[Tesla] Cycle complete, cooling down for {self.cooldown_seconds}s"
                )
                self._stop.wait(self.cooldown_seconds)

    def _poll_cycle(self):
        """Poll Tesla jobs and manage tracking."""
        logger.debug("[Tesla] Polling cycle started")

        all_jobs = self.adapter.fetch_all_jobs()
        if not all_jobs:
            logger.warning("[Tesla] No jobs fetched from Tesla")
            return

        all_ids = sorted({str(job.get("id", "")) for job in all_jobs if job.get("id")})
        logger.info(f"[Tesla] Fetched {len(all_ids)} job(s)")

        self._process_jobs(all_jobs, all_ids)

    def _process_jobs(self, jobs_list: List[dict], all_ids: List[str]):
        """Process batch of jobs: track IDs, detect new, handle removals."""
        endpoint = "tesla"  # Fixed endpoint for Tesla

        # Get current tracking state
        record = self.db.get_record(endpoint, "ats")
        seen_ids = record["seen_ids"] if record else []
        metadata = record.get("metadata", {}) if record else {}

        # First ever run — seed baseline silently, no alert
        if record is None:
            canonical = json.dumps(sorted(all_ids))
            new_hash = JobDatabase.compute_hash(canonical)
            self.db.update(endpoint, "ats", new_hash, all_ids)
            logger.info(
                f"[Tesla] 🌱 Baseline set — {len(all_ids)} job(s) recorded. Monitoring started."
            )
            return

        # Check for changes
        if set(seen_ids) == set(all_ids):
            logger.debug("[Tesla] ✓ Stable job set")
            return

        truly_new_ids = [jid for jid in all_ids if jid not in seen_ids]
        removed_ids_candidates = [jid for jid in seen_ids if jid not in all_ids]

        # Apply disappearance policy for all ATS types
        absent_counts = (
            metadata.get("disappearance_counts", {})
            if isinstance(metadata, dict)
            else {}
        )
        confirmed_removed = []
        updated_counts = {}

        for jid in removed_ids_candidates:
            remaining = absent_counts.get(jid, TESLA_DISAPPEARANCE_THRESHOLD)
            remaining -= 1
            if remaining <= 0:
                confirmed_removed.append(jid)
            else:
                updated_counts[jid] = remaining

        kept_ids = [jid for jid in seen_ids if jid not in confirmed_removed]
        metadata = {"disappearance_counts": updated_counts} if updated_counts else {}

        # Notify about new jobs
        if truly_new_ids:
            logger.info(f"[Tesla] 🚨 {len(truly_new_ids)} NEW job(s)!")
            new_jobs = self._fetch_new_job_details(truly_new_ids, jobs_list)
            if new_jobs:
                self.notifier.notify(new_jobs)

        if confirmed_removed:
            logger.info(
                f"[Tesla] ➖ {len(confirmed_removed)} job(s) removed: {confirmed_removed}"
            )

        # Update database — ONLY save current jobs (all_ids), don't merge with old seen_ids
        canonical = json.dumps(sorted(all_ids))
        new_hash = JobDatabase.compute_hash(canonical)
        self.db.update(endpoint, "ats", new_hash, all_ids, metadata=metadata)

    def _fetch_new_job_details(
        self, new_ids: List[str], jobs_list: List[dict]
    ) -> List[Job]:
        """Build Job objects from job details in the jobs list."""
        jobs = []

        # Create a mapping of IDs to job details
        id_to_details = {str(job.get("id", "")): job for job in jobs_list}

        for job_id in new_ids:
            details = id_to_details.get(job_id)
            if not details:
                logger.warning(f"[Tesla] No details found for job {job_id}")
                continue

            # Build apply URL from title and ID
            title = details.get("t", "")
            apply_url = TeslaAdapter._build_apply_url(title, job_id)

            job = self._build_job_from_details(details, apply_url)
            if job:
                jobs.append(job)

        logger.debug(
            f"[Tesla] Successfully built {len(jobs)} Job objects from {len(new_ids)} new jobs"
        )
        return jobs

    @staticmethod
    def _build_job_from_details(details: dict, apply_url: str) -> Optional[Job]:
        """Build a Job object from Tesla job details."""
        try:
            job_id = str(details.get("id", ""))
            if not job_id:
                return None

            return Job(
                id=job_id,
                title=details.get("t", "Untitled"),  # Tesla uses 't' for title
                company="Tesla",
                location=details.get("location", ""),
                department=details.get("department", "") or details.get("jobFamily", ""),
                url=apply_url,
                posted_at=None,  # Tesla doesn't provide publish date
                remote=(
                    "remote" in details.get("location", "").lower()
                    if details.get("location")
                    else False
                ),
                salary=None,  # Tesla doesn't include salary in job listing
                raw=details,
            )
        except Exception as e:
            logger.error(f"[Tesla] Failed to build Job object: {e}")
            return None
