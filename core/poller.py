"""
core/poller.py — Priority-wheel polling engine for Job Sniper.

ARCHITECTURE (v5 - with live config monitoring):
─────────────────────────────────────────────────────────────────────
  NEW: Live Config Monitor
  - Background thread checks for ATS setting changes every 2 seconds
  - When settings change, reloads eligible companies and reinitializes scheduler
  - Logs all changes clearly to console for user visibility
  - Gracefully handles scheduler reset without losing state

  Priority-Weighted Scheduler with Thread Pool
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
    • Config Monitor: 1 daemon thread (NEW) — watches for setting changes
    • HTTP calls: sequential per poll, concurrency limited by executor
─────────────────────────────────────────────────────────────────────
"""
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from core.config import Config
from core.database import JobDatabase
from core.http_client import HttpClient
from core.models import ATSType, Company, Priority
from ats import router as ats_router
from ats.ashby import RateLimitError
from notifications.notifier import Notifier
from core.scheduler import PriorityScheduler

logger = logging.getLogger("job_sniper.poller")

WORKDAY_DISAPPEARANCE_THRESHOLD = 5
ALL_ATS_DISAPPEARANCE_THRESHOLD = 3  # For other ATS types


def _compute_settings_hash(db: JobDatabase) -> str:
    """
    Compute a hash of all ATS, company-specific, and notification settings.
    Used to detect when user toggles an ATS on/off, company adapter on/off, or notification channel.
    """
    settings = {}
    
    # ATS settings
    for ats in ATSType:
        key = f"ats_{ats.value}"
        value = db.get_setting(key)
        settings[key] = value or "true"  # Default to enabled if not set
    
    # Company-specific settings (Google, Tesla)
    for company in ["google", "tesla"]:
        key = f"company_{company}"
        value = db.get_setting(key)
        settings[key] = value or "true"  # Default to enabled if not set
    
    # Notification channel settings
    for channel in ["console", "telegram", "webhook"]:
        key = f"notify_channel_{channel}"
        value = db.get_setting(key)
        settings[key] = value or "false"  # Default to disabled if not set
    
    # Notification rules enabled flag
    config_value = db.get_notification_config() or {}
    settings["notify_rules_enabled"] = str(config_value.get("enabled", False))
    
    settings_json = json.dumps(settings, sort_keys=True)
    return hashlib.md5(settings_json.encode()).hexdigest()


def _filter_enabled_companies(all_companies: List[Company], db: JobDatabase) -> List[Company]:
    """
    Filter companies to only include those with enabled ATS types.
    Company is included only if:
    1. Company is enabled AND
    2. NOT a company-specific adapter (Google, Tesla) AND
    3. Its ATS type is enabled (settings stored in DB)
    
    NOTE: Google and Tesla are independent pollers, not ATS companies.
    They are controlled separately via their own start/stop methods.
    """
    enabled_list = []
    for company in all_companies:
        if not company.enabled:
            continue
        
        # Skip company-specific adapters (Google, Tesla) — they're handled separately
        if company.board_token.lower() in ["google", "tesla"]:
            continue
        
        # Regular ATS-based companies
        ats_setting = db.get_setting(f"ats_{company.ats.value}")
        is_ats_enabled = ats_setting != "false"  # Default to True if not set
        if is_ats_enabled:
            enabled_list.append(company)
    
    return enabled_list


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────

class PollOrchestrator:
    """
    Uses PriorityScheduler to dispatch polls to ThreadPoolExecutor.
    Scales to thousands of companies with fixed worker threads.
    
    Features:
    - Dynamic config monitoring: detects ATS setting changes and reloads
    - Logs all setting changes to console for user visibility
    - Thread-safe scheduler reinitialization
    """

    def __init__(
        self,
        all_companies: List[Company],
        config: Config,
        db: JobDatabase,
        http: HttpClient,
        notifier: Notifier,
        google_poller=None,  # GooglePoller instance (optional)
        tesla_poller=None,   # TeslaPoller instance (optional)
    ):
        self.all_companies = all_companies  # All companies (before ATS filtering)
        self.companies = _filter_enabled_companies(all_companies, db)  # Currently enabled ATS companies only
        self.config   = config
        self.db       = db
        self.http     = http
        self.notifier = notifier
        self.google_poller = google_poller
        self.tesla_poller = tesla_poller
        self._google_enabled = None  # Track current state
        self._tesla_enabled = None   # Track current state
        self._stop    = threading.Event()
        self.scheduler = PriorityScheduler(self.companies, callback=self._log_stats)
        self.executor = ThreadPoolExecutor(max_workers=config.max_workers)
        self.dispatcher_thread = None
        self._heartbeat_thread = None
        self._config_monitor_thread = None
        self._heartbeat_lock = threading.Lock()
        self._scheduler_lock = threading.RLock()  # RLock for reentrant config updates
        self._last_polled_company: str = "none"
        self._polls_since_heartbeat = 0
        
        # Config monitoring
        self._last_settings_hash = _compute_settings_hash(db)
        self._last_settings_state = self._get_current_adapter_settings()  # Store actual settings for comparison
        
        # Initialize company poller states
        self._google_enabled = db.get_setting("company_google") != "false"
        self._tesla_enabled = db.get_setting("company_tesla") != "false"
        
        # Precompute ATS schemas for enabled companies
        self._update_ats_schemas()

    def _update_ats_schemas(self):
        """Update ATS schemas cache for current set of companies."""
        self.ats_schemas = {c.ats: self.config.get_ats_schema(c.ats) for c in self.companies}

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

        # NEW: Start config monitor thread
        self._config_monitor_thread = threading.Thread(target=self._monitor_config_changes, daemon=True)
        self._config_monitor_thread.start()

        logger.info("Dispatcher launched. Config monitor active. Press Ctrl+C to stop.\n")

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

    def _get_current_adapter_settings(self) -> dict:
        """Get current state of all adapter settings (ATS, company-specific, and notification)."""
        settings = {}
        
        # ATS adapter settings
        for ats in ATSType:
            key = f"ats_{ats.value}"
            value = self.db.get_setting(key)
            settings[key] = value != "false"  # Default to True if not set
        
        # Company-specific adapter settings
        for company in ["google", "tesla"]:
            key = f"company_{company}"
            value = self.db.get_setting(key)
            settings[key] = value != "false"  # Default to True if not set
        
        # Notification channel settings
        for channel in ["console", "telegram", "webhook"]:
            key = f"notify_channel_{channel}"
            value = self.db.get_setting(key)
            settings[key] = value != "false"  # Default to True if not set
        
        # Notification rules enabled flag
        config_value = self.db.get_notification_config() or {}
        settings["notify_rules_enabled"] = str(config_value.get("enabled", False))
        
        return settings

    def _monitor_config_changes(self):
        """
        Background thread that monitors for ATS setting changes.
        When changes are detected, reloads companies and reinitializes scheduler.
        Logs all changes to console for user visibility.
        """
        logger.debug("🔍 Config monitor thread started")
        
        while not self._stop.is_set():
            try:
                # Check every 2 seconds
                if self._stop.wait(2.0):
                    break
                
                # Compute current settings hash
                current_hash = _compute_settings_hash(self.db)
                
                if current_hash != self._last_settings_hash:
                    logger.info("=" * 70)
                    logger.info("🔄 [CONFIG UPDATE] Adapter settings changed!")
                    
                    # Fetch current settings from database
                    current_settings = self._get_current_adapter_settings()
                    old_settings = self._last_settings_state
                    
                    # Log individual adapter changes
                    logger.info("   📋 Adapter changes:")
                    adapter_changes = []
                    
                    for key in current_settings.keys():
                        if key.startswith("notify"):
                            continue  # Handle notification settings separately
                        
                        new_enabled = current_settings[key]
                        old_enabled = old_settings.get(key, True)  # Default to enabled if not in old settings
                        
                        if old_enabled != new_enabled:
                            # Extract adapter name and type
                            if key.startswith("ats_"):
                                adapter_name = key[4:].title()
                                adapter_type = "ATS"
                            else:  # company_*
                                adapter_name = key[8:].title()
                                adapter_type = "Company"
                            
                            status = "✅ ENABLED" if new_enabled else "❌ DISABLED"
                            logger.info(f"      {status} {adapter_name} ({adapter_type})")
                            adapter_changes.append((adapter_name, adapter_type, new_enabled))
                    
                    # Log notification channel changes
                    notify_channels_changed = False
                    for channel in ["console", "telegram", "webhook"]:
                        key = f"notify_channel_{channel}"
                        new_val = current_settings.get(key, False)
                        old_val = old_settings.get(key, False)
                        if new_val != old_val:
                            if not notify_channels_changed:
                                logger.info("   📢 Notification channel changes:")
                                notify_channels_changed = True
                            status = "✅ ENABLED" if new_val else "❌ DISABLED"
                            logger.info(f"      {status} {channel.title()}")
                    
                    # Log notification rules changes
                    rules_key = "notify_rules_enabled"
                    rules_changed = current_settings.get(rules_key, "False") != old_settings.get(rules_key, "False")
                    if rules_changed:
                        rules_enabled = current_settings.get(rules_key, "False") == "True"
                        status = "✅ ENABLED" if rules_enabled else "❌ DISABLED"
                        logger.info(f"   🔔 Notification rules: {status}")
                    
                    # Handle company-specific adapters (Google, Tesla) — start/stop pollers
                    self._update_company_poller_state(current_settings)
                    
                    # Get old and new enabled company lists (ATS companies only)
                    new_companies = _filter_enabled_companies(self.all_companies, self.db)
                    new_companies_set = set(new_companies)
                    
                    # Show affected companies
                    old_companies_set = set(self.companies)
                    added = new_companies_set - old_companies_set
                    removed = old_companies_set - new_companies_set
                    
                    if added or removed:
                        logger.info("   👥 Company changes:")
                        if added:
                            logger.info(f"      ✅ ENABLED: {len(added)} company(ies)")
                            for c in sorted(added, key=lambda x: x.name):
                                logger.info(f"         + {c.name}")
                        
                        if removed:
                            logger.info(f"      ❌ DISABLED: {len(removed)} company(ies)")
                            for c in sorted(removed, key=lambda x: x.name):
                                logger.info(f"         - {c.name}")
                    else:
                        logger.info("   👥 Company changes: (none — only adapter settings changed)")
                    
                    # Reinitialize scheduler with new company list
                    self._reload_companies_and_scheduler(new_companies)
                    
                    # Update settings hash AND store current settings for next comparison
                    self._last_settings_hash = current_hash
                    self._last_settings_state = current_settings.copy()
                    
                    logger.info(f"   📊 Active: {len(new_companies)} enabled ATS companies")
                    logger.info("=" * 70)
            
            except Exception as e:
                logger.error(f"💥 Error in config monitor: {e}", exc_info=True)

    def _reload_companies_and_scheduler(self, new_companies: List[Company]):
        """
        Safely reload the company list and reinitialize the scheduler.
        This is called when ATS settings change.
        Thread-safe: acquired lock to prevent dispatcher from accessing
        scheduler during reinitialization.
        """
        with self._scheduler_lock:
            try:
                old_count = len(self.companies)
                new_count = len(new_companies)
                
                # Update company list and ATS schemas
                self.companies = new_companies
                self._update_ats_schemas()
                
                # Reinitialize scheduler with new companies
                old_scheduler = self.scheduler
                self.scheduler = PriorityScheduler(self.companies, callback=self._log_stats)
                
                logger.info(f"   ✓ Scheduler reinitialized: {old_count} → {new_count} companies")
                if self.companies:
                    logger.debug(f"     Companies in new scheduler: {', '.join(c.name for c in sorted(self.companies, key=lambda x: x.name)[:5])}...")
            
            except Exception as e:
                logger.error(f"   ❌ Failed to reinitialize scheduler: {e}", exc_info=True)
                raise

    def _update_company_poller_state(self, current_settings: dict):
        """
        Start or stop Google and Tesla pollers based on their adapter settings.
        """
        # Handle Google adapter
        google_enabled = current_settings.get("company_google", False)
        if self.google_poller and google_enabled != self._google_enabled:
            if google_enabled:
                logger.info("   ▶️  Starting Google Careers poller")
                self.google_poller.start()
            else:
                logger.info("   ⏹️  Stopping Google Careers poller")
                self.google_poller.stop()
            self._google_enabled = google_enabled
        
        # Handle Tesla adapter
        tesla_enabled = current_settings.get("company_tesla", False)
        if self.tesla_poller and tesla_enabled != self._tesla_enabled:
            if tesla_enabled:
                logger.info("   ▶️  Starting Tesla Careers poller")
                self.tesla_poller.start()
            else:
                logger.info("   ⏹️  Stopping Tesla Careers poller")
                self.tesla_poller.stop()
            self._tesla_enabled = tesla_enabled

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
        
        Thread-safe: acquires scheduler_lock briefly to get next company, allowing
        config monitor to reinitialize scheduler without blocking long.
        """
        try:
            while not self._stop.is_set():
                # Acquire lock briefly to get next company from scheduler
                with self._scheduler_lock:
                    state = self.scheduler.next_company()
                # Lock released here — config monitor can update if needed

                if state is None:
                    # All companies cooling down — sleep until soonest is ready
                    with self._scheduler_lock:
                        sleep_for = self.scheduler.soonest_ready_in()
                    # Always sleep at least 0.1s to avoid busy-looping when scheduler is empty
                    sleep_for = max(sleep_for, 0.1)
                    logger.debug(f"Dispatcher: no work available, sleeping {sleep_for:.2f}s")
                    self._stop.wait(sleep_for)  # interruptible
                    continue

                try:
                    self.executor.submit(self._poll_company, state)
                except RuntimeError:
                    break  # Executor shut down

                # Inter-dispatch gap — prevents bursting all HIGH slots at once
                with self._scheduler_lock:
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
        
        # Frequency inspection: Log when a specific board token is polled
        if (self.config.frequency_inspection_enabled and 
            self.config.frequency_inspect_board_token and
            company.board_token == self.config.frequency_inspect_board_token):
            logger.info(f"[FREQ_INSPECT] {company.board_token} dispatched (slot #{self._polls_since_heartbeat})")
        
        try:
            # Check if this company's ATS is still enabled
            # (it might have been disabled between scheduler.next_company() and now)
            schema = self.ats_schemas.get(company.ats)
            if schema is None:
                logger.debug(f"[{company.name}] Skipped (ATS {company.ats.value} no longer enabled)")
                # Record as success to avoid throttling due to a transient disable
                with self._scheduler_lock:
                    self.scheduler.record_outcome(state, True)
                return
            
            self._poll_once(company)
            # Acquire lock to record outcome in scheduler
            with self._scheduler_lock:
                self.scheduler.record_outcome(state, True)
        except RateLimitError as e:
            # Rate-limited: apply aggressive global backoff
            logger.error(f"[{company.name}] Rate limit hit: {e}")
            # Trigger global slowdown by recording failure with rate limit flag
            with self._scheduler_lock:
                self.scheduler.record_outcome(state, False, is_rate_limit=True)
        except Exception as e:
            logger.warning(f"[{company.name}] Error: {e}")
            with self._scheduler_lock:
                self.scheduler.record_outcome(state, False)

    def _poll_once(self, company):
        schema = self.ats_schemas.get(company.ats)
        if schema is None:
            # This should normally not happen due to the check in _poll_company,
            # but keeping it here as a safety net
            logger.debug(f"[{company.name}] Skipped during poll (schema not found for {company.ats.value})")
            return

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
        if self._config_monitor_thread:
            self._config_monitor_thread.join(timeout=2)
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
            with self._scheduler_lock:
                gap = self.scheduler.adaptive_gap
            logger.info(
                f"💓 Heartbeat: companies={stats['total_tracked_companies']} "
                f"total_jobs={stats['total_seen_jobs']} "
                f"gap={gap:.2f}s "
                f"last={last_polled} polls={polls}"
            )

    def _print_summary(self):
        print("\n" + "═" * 62)
        print("  JOB SNIPER — Priority Scheduler Monitor")
        print("═" * 62)
        print(self.scheduler.summary())
        print(f"\n  max_workers = {self.config.max_workers}")
        print("═" * 62 + "\n")
