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

from bs4 import BeautifulSoup
from core.database import JobDatabase
from core.models import Job
from core.description_parser import parse_html_description
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
        if self._thread.is_alive():
            logger.debug("[tesla] Already running, ignoring start()")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        logger.info("🚀 Starting Tesla Careers poller")
        self._thread.start()

    def stop(self):
        logger.info("⏹ Stopping Tesla Careers poller…")
        self._stop.set()
        if self._thread.is_alive():
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
            try:
                new_jobs = self._fetch_new_job_details(truly_new_ids, jobs_list)
                logger.info(f"[Tesla] [{len(new_jobs)}/{len(truly_new_ids)}] jobs successfully fetched")
                
                if new_jobs:
                    self.notifier.notify(new_jobs)
                else:
                    logger.warning(f"[Tesla] Failed to fetch details for any of the {len(truly_new_ids)} new jobs")
            except Exception as e:
                logger.error(f"[Tesla] Error fetching/notifying new jobs: {e}", exc_info=True)

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
        """
        Fetch detailed metadata for new Tesla jobs and build Job objects.
        
        Falls back to partial job data if detail fetching fails, but still 
        forwards the job to notification pipeline. Better to notify with 
        incomplete data than drop the job entirely.
        """
        jobs = []
        
        # Map job IDs to base job info from initial fetch
        id_to_base_job = {
            str(job.get("id", "")): job
            for job in jobs_list
        }
        
        id_to_url = {
            str(job.get("id", "")): job.get("apply_url", "")
            for job in jobs_list
        }

        job_urls = [id_to_url[job_id] for job_id in new_ids if id_to_url.get(job_id)]
        if not job_urls:
            logger.warning("[Tesla] No apply URLs found for new jobs")
            return []

        logger.debug(f"[Tesla] Fetching details for {len(job_urls)} new job(s)")
        details_by_url = self.adapter.fetch_job_details_batch(job_urls)

        for job_id in new_ids:
            apply_url = id_to_url.get(job_id)
            if not apply_url:
                logger.warning(f"[Tesla] No URL available for new job {job_id}")
                continue

            base_job = id_to_base_job.get(job_id)
            details = details_by_url.get(apply_url)
            
            if details:
                # Full details available
                job = self._build_job_from_details(details, apply_url)
                if job:
                    jobs.append(job)
            elif base_job:
                # Detail fetch failed, but build from base info
                logger.warning(f"[Tesla] Failed to fetch full details for job {job_id}; using partial data")
                job = self._build_job_from_base_info(base_job, apply_url)
                if job:
                    jobs.append(job)
            else:
                logger.warning(f"[Tesla] No data available for job {job_id}")

        logger.debug(
            f"[Tesla] Successfully built {len(jobs)} Job objects from {len(new_ids)} new jobs "
            f"({len([j for j in jobs if j.description])} with full details, "
            f"{len([j for j in jobs if not j.description])} with partial data)"
        )
        return jobs

    @staticmethod
    def _build_job_from_base_info(base_job: dict, apply_url: str) -> Optional[Job]:
        """
        Build a Job object from base info only (no description).
        
        Used as fallback when detail fetching fails. Better to notify 
        with partial data than drop the job entirely.
        """
        try:
            job_id = str(base_job.get("id", ""))
            if not job_id:
                return None

            title = base_job.get("t") or "Untitled"
            location = base_job.get("location") or ""

            return Job(
                id=job_id,
                title=title,
                company="Tesla",
                location=location,
                department="",
                url=apply_url,
                posted_at=None,
                remote=("remote" in location.lower() if location else False),
                salary=None,
                description=None,  # No description available
                raw=base_job,
            )
        except Exception as e:
            logger.error(f"[Tesla] Failed to build partial Job object: {e}")
            return None

    @staticmethod
    def _build_tesla_description(details: dict) -> Optional[str]:
        """
        Build complete job description from Tesla job details.
        
        Assembles: What to Expect + What You'll Do + What You'll Bring + Compensation and Benefits
        
        Each section is parsed from HTML to plain text.
        """
        parts = []
        
        # Part 1: What to Expect (jobDescription)
        job_description = details.get("jobDescription", "")
        if job_description and job_description.strip():
            parsed = parse_html_description(job_description)
            if parsed:
                parts.append(f"What to Expect:\n{parsed}")
        
        # Part 2: What You'll Do (jobResponsibilities)
        job_responsibilities = details.get("jobResponsibilities", "")
        if job_responsibilities and job_responsibilities.strip():
            parsed = parse_html_description(job_responsibilities)
            if parsed:
                parts.append(f"What You'll Do:\n{parsed}")
        
        # Part 3: What You'll Bring (jobRequirements)
        job_requirements = details.get("jobRequirements", "")
        if job_requirements and job_requirements.strip():
            parsed = parse_html_description(job_requirements)
            if parsed:
                parts.append(f"What You'll Bring:\n{parsed}")
        
        # Part 4: Compensation and Benefits (jobCompensationAndBenefits) | Already included in compensation part - no need to repeat in job-description.
        # job_comp_benefits = details.get("jobCompensationAndBenefits", "")
        # if job_comp_benefits and job_comp_benefits.strip():
        #     parsed = parse_html_description(job_comp_benefits)
        #     if parsed:
        #         parts.append(f"Compensation and Benefits:\n{parsed}")
        
        # Assemble final description
        if parts:
            return "\n\n".join(parts)
        
        return None

    @staticmethod
    def _build_job_from_details(details: dict, apply_url: str) -> Optional[Job]:
        """Build a Job object from Tesla job details."""
        try:
            job_id = str(details.get("id", ""))
            if not job_id:
                return None

            title = details.get("title") or details.get("t") or "Untitled"
            location = details.get("location") or ""
            department = (
                details.get("department")
                or details.get("jobFamily")
                or details.get("subWorkerType")
                or ""
            )
            compensation = details.get("jobCompensationAndBenefits") or details.get("salary") or None

            # Parse HTML compensation to readable text
            if compensation and isinstance(compensation, str) and '<' in compensation:
                soup = BeautifulSoup(compensation, 'html.parser')
                # Extract text, clean up extra whitespace
                compensation = ' '.join(soup.get_text().split())

            return Job(
                id=job_id,
                title=title,
                company="Tesla",
                location=location,
                department=department,
                url=apply_url,
                posted_at=None,  # Tesla doesn't provide publish date
                remote=("remote" in location.lower() if location else False),
                salary=compensation,
                description=TeslaPoller._build_tesla_description(details),
                raw=details,
            )
        except Exception as e:
            logger.error(f"[Tesla] Failed to build Job object: {e}")
            return None
