"""
core/apple_poller.py — Dedicated poller for Apple Careers

This poller runs independently, fetching jobs from Apple Careers API
at a configured cooldown interval, filtering by last 6 hours,
tracking job IDs, and notifying on new postings.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from core.database import JobDatabase
from core.models import Job
from core.description_parser import parse_html_description
from notifications.notifier import Notifier
from company.apple.apple import AppleAdapter, AppleJobDetailFetcher

logger = logging.getLogger("job_sniper.apple_poller")

APPLE_DISAPPEARANCE_THRESHOLD = 3  # Remove job if missing from 3 consecutive polls


class ApplePoller:
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
        self.adapter = AppleAdapter(timeout=request_timeout)
        self.detail_fetcher = AppleJobDetailFetcher(timeout=request_timeout)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self):
        if self._thread.is_alive():
            logger.debug("[apple] Already running, ignoring start()")
            return
        # Re-create thread so the poller can be restarted after a previous stop().
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        logger.info("🚀 Starting Apple Careers poller")
        self._thread.start()

    def stop(self):
        logger.info("⏹ Stopping Apple Careers poller…")
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._poll_cycle()
            except Exception as e:
                logger.error(f"[apple] Polling cycle error: {e}", exc_info=True)
                time.sleep(60)  # Wait a minute before retrying on error

            if not self._stop.is_set():
                logger.info(
                    f"[apple] Cycle complete, cooling down for {self.cooldown_seconds}s"
                )
                self._stop.wait(self.cooldown_seconds)

    def _poll_cycle(self):
        """Poll Apple jobs and manage tracking."""
        logger.debug("[apple] Polling cycle started")

        # Fetch all recent jobs (last 6 hours)
        all_jobs, total_records = self.adapter.fetch_all_recent_jobs(hours=6)
        if not all_jobs:
            logger.warning("[apple] No recent jobs fetched from Apple")
            return

        all_ids = sorted({str(job.get("id", "")) for job in all_jobs if job.get("id")})
        logger.info(f"[apple] Fetched {len(all_ids)} recent job(s) from {total_records} total")

        self._process_jobs(all_jobs, all_ids)

    def _process_jobs(self, jobs_list: List[dict], all_ids: List[str]):
        """Process batch of jobs: track IDs, detect new, handle removals."""
        endpoint = "apple"  # Fixed endpoint for Apple

        # Get current tracking state
        record = self.db.get_record(endpoint, "company")
        seen_ids = record["seen_ids"] if record else []
        metadata = record.get("metadata", {}) if record else {}

        # First ever run — seed baseline silently, no alert
        if record is None:
            canonical = json.dumps(sorted(all_ids))
            new_hash = JobDatabase.compute_hash(canonical)
            self.db.update(endpoint, "company", new_hash, all_ids)
            logger.info(
                f"[apple] 🌱 FIRST RUN: Baseline set — {len(all_ids)} job(s) recorded. Monitoring started. "
                f"New jobs will be detected and notified from the next poll cycle onwards."
            )
            return

        # Check for changes
        if set(seen_ids) == set(all_ids):
            logger.debug("[apple] ✓ Stable job set")
            return

        truly_new_ids = [jid for jid in all_ids if jid not in seen_ids]
        removed_ids_candidates = [jid for jid in seen_ids if jid not in all_ids]

        # Apply disappearance policy
        absent_counts = (
            metadata.get("disappearance_counts", {})
            if isinstance(metadata, dict)
            else {}
        )
        confirmed_removed = []
        updated_counts = {}

        for jid in removed_ids_candidates:
            remaining = absent_counts.get(jid, APPLE_DISAPPEARANCE_THRESHOLD)
            remaining -= 1
            if remaining <= 0:
                confirmed_removed.append(jid)
            else:
                updated_counts[jid] = remaining

        kept_ids = [jid for jid in seen_ids if jid not in confirmed_removed]
        metadata = {"disappearance_counts": updated_counts} if updated_counts else {}

        # Notify about new jobs
        if truly_new_ids:
            logger.info(f"[apple] 🚨 {len(truly_new_ids)} NEW job(s)!")
            try:
                new_jobs = self._fetch_new_job_details(truly_new_ids, jobs_list)
                logger.info(f"[apple] [{len(new_jobs)}/{len(truly_new_ids)}] jobs successfully fetched")
                
                if new_jobs:
                    self.notifier.notify(new_jobs)
                else:
                    logger.warning(f"[apple] Failed to fetch details for any of the {len(truly_new_ids)} new jobs")
            except Exception as e:
                logger.error(f"[apple] Error fetching/notifying new jobs: {e}", exc_info=True)

        if confirmed_removed:
            logger.info(
                f"[apple] ➖ {len(confirmed_removed)} job(s) removed: {confirmed_removed}"
            )

        # Update database — ONLY save current jobs (all_ids)
        canonical = json.dumps(sorted(all_ids))
        new_hash = JobDatabase.compute_hash(canonical)
        self.db.update(endpoint, "company", new_hash, all_ids, metadata=metadata)

    def _fetch_new_job_details(
        self, new_ids: List[str], jobs_list: List[dict]
    ) -> List[Job]:
        """Fetch detailed metadata for new Apple jobs and build Job objects."""
        jobs = []

        id_to_job = {
            str(job.get("id", "")): job
            for job in jobs_list
        }

        logger.debug(f"[apple] Fetching details for {len(new_ids)} new job(s)")

        # Fetch details for all new jobs in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self.detail_fetcher.fetch_job_detail, id_to_job[job_id].get("positionId")): job_id
                for job_id in new_ids
                if job_id in id_to_job
            }

            for future in as_completed(futures):
                job_id = futures[future]
                base_job = id_to_job.get(job_id)
                
                try:
                    job_details = future.result()
                    if job_details:
                        job = self._build_job_from_details(base_job, job_details)
                        if job:
                            jobs.append(job)
                except Exception as e:
                    logger.warning(f"[apple] Error fetching details for job {job_id}: {e}")

        logger.debug(
            f"[apple] Successfully built {len(jobs)} Job objects from {len(new_ids)} new jobs"
        )
        return jobs

    @staticmethod
    def _build_job_from_details(base_job: dict, job_details: dict) -> Optional[Job]:
        """Build a Job object from Apple job base info and details."""
        try:
            job_id = str(base_job.get("id", ""))
            if not job_id:
                return None

            position_id = base_job.get("positionId", "")
            title = base_job.get("postingTitle", "Untitled")
            location = AppleAdapter.extract_location(base_job)
            url = f"https://jobs.apple.com/en-us/details/{position_id}"
            remote = AppleAdapter.is_remote(base_job)

            # Extract team as department
            team = base_job.get("team", {})
            department = team.get("teamName", "") if isinstance(team, dict) else ""

            # Build description from job details
            description = AppleJobDetailFetcher.build_job_description(job_details)

            job = Job(
                id=job_id,
                title=title,
                company="Apple",
                location=location,
                department=department,
                url=url,
                remote=remote,
                description=description,
                raw=base_job,
            )

            return job
        except Exception as e:
            logger.warning(f"[apple] Failed to build Job object: {e}")
            return None
