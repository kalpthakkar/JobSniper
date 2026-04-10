"""
core/poller.py — Priority-wheel polling engine for Job Sniper.

ARCHITECTURE (v4):
─────────────────────────────────────────────────────────────────────
  NEW: Priority-Weighted Scheduler with Thread Pool
  - Single dispatcher thread calls scheduler.next_company()
  - Submits to ThreadPoolExecutor(max_workers) for polling
  - Scheduler handles priority weighting and adaptive rate limiting
  - No more one-thread-per-company (scales to 1000s of companies)
  - Semaphore removed; executor limits concurrency

  Polling cycle:
    fetch() → hash check → (if changed) extract_new_jobs() → notify → db update

  Threading model:
    • Dispatcher: 1 daemon thread
    • Workers: ThreadPoolExecutor(max_workers) — polls run here
    • HTTP calls: sequential per poll, concurrency limited by executor
─────────────────────────────────────────────────────────────────────
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

from core.config import Config
from core.database import JobDatabase
from core.http_client import HttpClient
from core.models import ATSType, Company
from ats import router as ats_router
from ats.ashby import RateLimitError
from notifications.notifier import Notifier
from core.scheduler import PriorityScheduler

logger = logging.getLogger("job_sniper.poller")

WORKDAY_DISAPPEARANCE_THRESHOLD = 5
ALL_ATS_DISAPPEARANCE_THRESHOLD = 3  # For other ATS types


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────

class PollOrchestrator:
    """
    Uses PriorityScheduler to dispatch polls to ThreadPoolExecutor.
    Scales to thousands of companies with fixed worker threads.
    """

    def __init__(self, companies: List[Company], config: Config, db: JobDatabase, http: HttpClient, notifier: Notifier):
        self.companies = companies
        self.config   = config
        self.db       = db
        self.http     = http
        self.notifier = notifier
        self._stop    = threading.Event()
        self.scheduler = PriorityScheduler(companies, callback=self._log_stats)
        self.executor = ThreadPoolExecutor(max_workers=config.max_workers)
        self.dispatcher_thread = None
        self._heartbeat_thread = None
        self._heartbeat_lock = threading.Lock()
        self._last_polled_company: str = "none"
        self._polls_since_heartbeat = 0
        # Precompute ATS schemas
        self.ats_schemas = {c.ats: config.get_ats_schema(c.ats) for c in companies}

    def start(self):
        logger.info(
            f"🚀 Job Sniper starting — "
            f"{len(self.companies)} companies | "
            f"max_workers={self.config.max_workers}"
        )
        self._print_summary()

        self.dispatcher_thread = threading.Thread(target=self._dispatch, daemon=True)
        self.dispatcher_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        logger.info("Dispatcher launched. Press Ctrl+C to stop.\n")

        try:
            while not self._stop.is_set():
                self._stop.wait(1)  # Allow interruption without fixed delay
        except KeyboardInterrupt:
            logger.info("📢 Received Ctrl+C — initiating graceful shutdown…")
            self.stop()
        except Exception as e:
            logger.error(f"💥 Unexpected error in main loop: {e}", exc_info=True)
            self.stop()
            raise

    def _apply_disappearance_policy(self, company: Company, seen_ids: List[str], all_ids: List[str], metadata: dict) -> tuple[list[str], list[str], dict]:
        absent_counts = metadata.get("disappearance_counts", {}) if isinstance(metadata, dict) else {}
        threshold = WORKDAY_DISAPPEARANCE_THRESHOLD if company.ats == ATSType.WORKDAY else ALL_ATS_DISAPPEARANCE_THRESHOLD

        missing_ids = [jid for jid in seen_ids if jid not in all_ids]
        confirmed_removed = []
        updated_counts = {}

        for jid in missing_ids:
            remaining = absent_counts.get(jid, threshold)
            remaining -= 1
            if remaining <= 0:
                confirmed_removed.append(jid)
            else:
                updated_counts[jid] = remaining

        kept_ids = [jid for jid in seen_ids if jid not in confirmed_removed]
        metadata = {"disappearance_counts": updated_counts} if updated_counts else {}
        return kept_ids, confirmed_removed, metadata

    def _dispatch(self):
        """
        Dispatcher loop: pulls companies from scheduler and submits to executor.

        When all companies are in cooldown (next_company() → None), we sleep
        precisely until the soonest one is ready — not a fixed adaptive_gap.
        The adaptive_gap still controls inter-dispatch pacing when work IS available
        to prevent thundering-herd bursts.
        """
        try:
            while not self._stop.is_set():
                state = self.scheduler.next_company()

                if state is None:
                    # All companies cooling down — sleep until soonest is ready
                    sleep_for = self.scheduler.soonest_ready_in()
                    if sleep_for > 0.01:
                        logger.debug(f"Dispatcher: all in cooldown, sleeping {sleep_for:.2f}s")
                        self._stop.wait(sleep_for)  # interruptible
                    continue

                try:
                    self.executor.submit(self._poll_company, state)
                except RuntimeError:
                    break  # Executor shut down

                # Inter-dispatch gap — prevents bursting all HIGH slots at once
                gap = self.scheduler.adaptive_gap
                if gap > 0.01:
                    self._stop.wait(gap)  # interruptible by stop()

        except Exception as e:
            logger.error(f"💥 Dispatcher thread crashed: {e}", exc_info=True)
            self._stop.set()

    def _poll_company(self, state):
        company = state.company
        with self._heartbeat_lock:
            self._last_polled_company = company.name
            self._polls_since_heartbeat += 1
        try:
            self._poll_once(company)
            self.scheduler.record_outcome(state, True)
        except RateLimitError as e:
            # Rate-limited: apply aggressive global backoff
            logger.error(f"[{company.name}] Rate limit hit: {e}")
            # Trigger global slowdown by recording failure with rate limit flag
            self.scheduler.record_outcome(state, False, is_rate_limit=True)
        except Exception as e:
            logger.warning(f"[{company.name}] Error: {e}")
            self.scheduler.record_outcome(state, False)

    def _poll_once(self, company):
        schema = self.ats_schemas[company.ats]

        # ── HTTP call 1: fetch for hash/ID check ──────────────────────
        # Check if 24h filter is disabled for this ATS type
        disable_filter = self.config.disable_24h_filter.get(company.ats.value, False)
        raw_text, all_ids = ats_router.fetch(company, self.http, schema, disable_filter=disable_filter)

        # ── Pure logic ────────────────────────────
        new_hash = JobDatabase.compute_hash(raw_text)

        if not self.db.has_changed(company.board_token, company.ats.value, new_hash):
            logger.debug(f"[{company.name}] ✓ No change")
            return

        existing = self.db.get_record(company.board_token, company.ats.value)

        # First ever run → seed baseline silently, no alert
        if existing is None:
            self.db.update(company.board_token, company.ats.value, new_hash, all_ids)
            logger.info(
                f"[{company.name}] 🌱 Baseline set — "
                f"{len(all_ids)} job(s) recorded. Monitoring started."
            )
            return

        seen_ids = existing.get("seen_ids", [])
        metadata = existing.get("metadata", {})

        kept_ids, removed_ids, metadata = self._apply_disappearance_policy(company, seen_ids, all_ids, metadata)

        truly_new_ids = [jid for jid in all_ids if jid not in seen_ids]

        # Hash changed but no new/removed IDs = description edit
        if not truly_new_ids and not removed_ids and set(seen_ids) == set(all_ids):
            logger.info(f"[{company.name}] ↻ Hash changed, no ID changes (edit)")
            self.db.update(company.board_token, company.ats.value, new_hash, kept_ids, metadata=metadata)
            return

        # ── HTTP call 2: enrich new jobs ──────────────────────────────
        # Only when there are truly new IDs
        if truly_new_ids:
            # Check if 24h filter is disabled for this ATS type
            disable_filter = self.config.disable_24h_filter.get(company.ats.value, False)
            new_jobs = ats_router.extract_new_jobs(company, self.http, schema, seen_ids, disable_filter=disable_filter)
            if new_jobs:
                logger.info(f"[{company.name}] 🚨 {len(new_jobs)} NEW job(s)!")
                self.notifier.notify(new_jobs)

        if removed_ids:
            logger.info(f"[{company.name}] ➖ {len(removed_ids)} job(s) removed: {removed_ids}")

        merged = list(set(kept_ids) | set(all_ids))
        self.db.update(company.board_token, company.ats.value, new_hash, merged, metadata=metadata)

    def stop(self):
        logger.info("⏹  Shutting down Job Sniper…")
        self._stop.set()
        # cancel_futures=True drops queued (not yet started) work immediately.
        # wait=False means we don't block on in-flight requests — daemon threads
        # will be killed when the process exits. Without this, a single hung
        # Lever connection (ReadTimeout with 3 retries) would freeze shutdown
        # for up to 3 × timeout seconds.
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.dispatcher_thread:
            self.dispatcher_thread.join(timeout=2)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        logger.info("✓ Shutdown complete.")

    def _log_stats(self):
        stats = self.db.stats()
        logger.info(
            f"📊 companies_tracked={stats['total_tracked_companies']} | "
            f"total_jobs_seen={stats['total_seen_jobs']}"
        )

    def _heartbeat_loop(self):
        heartbeat_interval = 30.0
        while not self._stop.is_set():
            if self._stop.wait(heartbeat_interval):
                break
            stats = self.db.stats()
            with self._heartbeat_lock:
                last_polled = self._last_polled_company
                polls = self._polls_since_heartbeat
                self._polls_since_heartbeat = 0
            logger.info(
                f"💓 Heartbeat: companies={stats['total_tracked_companies']} "
                f"total_jobs={stats['total_seen_jobs']} "
                f"gap={self.scheduler.adaptive_gap:.2f}s "
                f"last={last_polled} polls={polls}"
            )

    def _print_summary(self):
        print("\n" + "═" * 62)
        print("  JOB SNIPER — Priority Scheduler Monitor")
        print("═" * 62)
        print(self.scheduler.summary())
        print(f"\n  max_workers = {self.config.max_workers}")
        print("═" * 62 + "\n")
