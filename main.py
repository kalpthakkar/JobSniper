#!/usr/bin/env python3
"""
main.py — Job Sniper entry point.

Usage:
  python main.py                    # Start monitoring with config.yaml
  python main.py --config my.yaml   # Use a custom config file
  python main.py --company stripe   # Probe a single company once (debug)
  python main.py --list             # List all configured companies
"""
import argparse
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import List
import queue
import yaml

from core.config import Config
from core.database import JobDatabase
from core.models import Company
from core.http_client import HttpClient
from core.poller import PollOrchestrator
from core.google_poller import GooglePoller
from core.tesla_poller import TeslaPoller
from core.apple_poller import ApplePoller
from core.microsoft_poller import MicrosoftPoller
from notifications.notifier import Notifier

logger = logging.getLogger("job_sniper")


# ─────────────────────────────────────────────────────────────
# Logging setup (thread-safe with queue handler)
# ─────────────────────────────────────────────────────────────
class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """
    Drop log records silently when the queue is full instead of blocking.

    The default QueueHandler.emit() calls queue.put() which BLOCKS when the
    queue is at capacity.  With 20+ worker threads all logging at INFO level
    across 8000+ companies, the bounded queue fills quickly; every thread then
    blocks inside logging, the ThreadPoolExecutor saturates, the dispatcher
    stalls, and the whole program freezes indefinitely.

    By switching to put_nowait() and discarding on Full we ensure worker threads
    are never stalled by the logging subsystem.  A counter tracks dropped records
    so we can surface the information without re-blocking.
    """

    _dropped: int = 0

    def enqueue(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            NonBlockingQueueHandler._dropped += 1
            # Every 1000 drops emit a single warning directly to stderr
            # (bypassing the queue entirely so it always appears).
            if NonBlockingQueueHandler._dropped % 1000 == 0:
                import sys
                print(
                    f"[WARNING] Logging queue full — {NonBlockingQueueHandler._dropped} "
                    "records dropped total. Consider reducing log verbosity.",
                    file=sys.stderr,
                    flush=True,
                )


def setup_logging(level: str):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    
    # Configure stdout for UTF-8 encoding (Windows compatibility)
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        except (AttributeError, ValueError):
            # Fallback if reconfigure fails
            pass
    
    # Use an unbounded queue so the listener never applies back-pressure.
    # Memory cost is negligible: a log record is ~1 KB; even 50 000 queued
    # records is only ~50 MB, far less than the RAM exhausted by blocking threads.
    log_queue: queue.Queue = queue.Queue()  # unbounded — listener drains faster than workers produce
    queue_handler = NonBlockingQueueHandler(log_queue)
    
    # Create a listener that drains the queue and writes to console with UTF-8 encoding
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    listener = logging.handlers.QueueListener(log_queue, stream_handler, respect_handler_level=True)
    listener.start()
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    root_logger.addHandler(queue_handler)
    
    # Suppress noisy but harmless urllib3 connection pool warnings
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    # Suppress per-request debug noise from these high-volume loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    
    return listener  # Return listener so we can stop it on shutdown


# ─────────────────────────────────────────────────────────────
# CLI args
# ─────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Job Sniper — Real-time job posting monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                Start full monitoring loop
  python main.py --config my.yaml               Use alternate config
  python main.py --company stripe               One-shot probe Stripe (debug)
  python main.py --list                         Show all tracked companies
  python main.py --dashboard                    Run web dashboard
  python main.py --token-start 0 --token-stop 4000    Multi-instance: poll tokens [0:4000]
  python main.py --token-start 4000 --token-stop 8000 Multi-instance: poll tokens [4000:8000]
        """,
    )
    parser.add_argument("--config",  default="config.yaml", help="Path to config file")
    parser.add_argument("--company", default=None,          help="One-shot probe a single board_token")
    parser.add_argument("--list",    action="store_true",   help="List all companies in database and exit")
    parser.add_argument("--dashboard", action="store_true", help="Run web dashboard for company management")
    parser.add_argument("--token-start", type=int, default=None, help="Token window start index (1-based inclusive) for multi-instance polling")
    parser.add_argument("--token-stop", type=int, default=None, help="Token window stop index (1-based inclusive) for multi-instance polling")
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────
# Filter companies by enabled ATS types
# ─────────────────────────────────────────────────────────────
def get_enabled_companies(companies: List[Company], db: JobDatabase, token_start: int = None, token_stop: int = None) -> List[Company]:
    """
    Filter companies to only include those with enabled ATS types.
    Company is included only if:
    1. Company is enabled AND
    2. Its ATS type is enabled (settings stored in DB)
    3. (Optional) Within token_start:token_stop window for multi-instance setup
    """
    filtered = []
    for company in companies:
        if not company.enabled:
            continue

        ats_setting = db.get_setting(f"ats_{company.ats.value}")
        if ats_setting == "false":
            continue

        filtered.append(company)

    filtered.sort(key=lambda c: c.board_token)

    if token_start is None and token_stop is None:
        return filtered

    start = token_start if token_start is not None else 1
    stop = token_stop if token_stop is not None else len(filtered)

    if start < 1:
        start = 1
    if stop < start:
        logger.warning(
            f"🪟 Token window empty: start={token_start} stop={token_stop} "
            f"(after normalization: [{start}:{stop}]). No companies will be polled."
        )
        return []

    start_idx = start - 1
    stop_idx = min(stop, len(filtered))
    windowed_companies = filtered[start_idx:stop_idx]

    logger.info(
        f"🪟 Token window enabled: [{start}:{stop}] (1-based inclusive) "
        f"→ selected {len(windowed_companies)} of {len(filtered)} enabled ATS companies"
    )

    return windowed_companies


# ─────────────────────────────────────────────────────────────
# One-shot probe (for debugging a single company)
# ─────────────────────────────────────────────────────────────
def probe_company(board_token: str, companies: List[Company], config: Config, http: HttpClient):
    from ats import router as ats_router

    match = next((c for c in companies if c.board_token == board_token), None)
    if not match:
        print(f"❌ Company with board_token='{board_token}' not found in database.")
        sys.exit(1)

    schema = config.get_ats_schema(match.ats)
    print(f"\n🔍 Probing {match.name} ({match.ats.value}) …\n")

    try:
        raw_text, ids = ats_router.fetch(match, http, schema)
        hash_val = JobDatabase.compute_hash(raw_text)
        print(f"✅ Fetched successfully")
        print(f"   Job count : {len(ids)}")
        print(f"   Hash      : {hash_val[:16]}…")
        print(f"   Sample IDs: {ids[:5]}")
        if ids:
            jobs = ats_router.extract_new_jobs(match, http, schema, [])
            if jobs:
                print(f"\n   First job preview:")
                print(f"   {jobs[0].short_repr()}")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # Load config first so we can get log level
    if not Path(args.config).exists():
        print(f"❌ Config file not found: {args.config}")
        sys.exit(1)

    config = Config(args.config)
    listener = setup_logging(config.log_level)
    logger = logging.getLogger("job_sniper.main")

    # Shared components
    http = HttpClient(
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        proxy_file=config.proxy_file,
        ip_strategy=config.ip_strategy,
    )
    db = JobDatabase(config.db_path)

    # Load companies from DB; if config still contains legacy entries, migrate them once.
    companies = db.get_companies()
    if not companies:
        raw = yaml.safe_load(Path(args.config).read_text())
        for item in raw.get("companies", []):
            db.add_company(item["board_token"], item["ats"], item["priority"])
        companies = db.get_companies()

    # ------ --list ------
    if args.list:
        enabled_list = get_enabled_companies(companies, db, args.token_start, args.token_stop)
        print(f"\nTracked companies ({len(enabled_list)} enabled of {len(companies)} total):\n")
        for c in enabled_list:
            print(f"  [{c.priority.value:4s}] {c.name:30s} | ATS: {c.ats.value:12s} | token: {c.board_token}")
        print()
        sys.exit(0)

    # ------ --dashboard ------
    if args.dashboard:
        from web.dashboard import app
        print("Starting web dashboard at http://127.0.0.1:5000")
        app.run(host='0.0.0.0', port=5000, debug=True)
        sys.exit(0)

    # ------ --company (one-shot debug) ------
    if args.company:
        probe_company(args.company, companies, config, http)
        sys.exit(0)

    # ------ Full monitoring loop ------
    # Apply token window if specified (for multi-instance distributed setup)
    enabled_companies = get_enabled_companies(companies, db, args.token_start, args.token_stop)
    google_enabled = db.get_setting("company_google") != "false"
    tesla_enabled = db.get_setting("company_tesla") != "false"
    apple_enabled = db.get_setting("company_apple") != "false"
    microsoft_enabled = db.get_setting("company_microsoft") != "false"
    
    # Allow start if either ATS companies OR company adapters are enabled
    if not enabled_companies and not google_enabled and not tesla_enabled and not apple_enabled and not microsoft_enabled:
        logger.error("❌ No enabled monitoring sources found. Enable at least one ATS type or company adapter in the dashboard.")
        sys.exit(1)
    
    logger.info(f"✓ Starting with {len(enabled_companies)} enabled ATS companies (of {len(companies)} total)")
    
    # Initialize notification channel settings from config (if not already set)
    # This allows the UI to toggle channels in real-time
    for channel in config.notify_channels:
        setting_key = f"notify_channel_{channel}"
        if db.get_setting(setting_key) is None:
            db.set_setting(setting_key, "true")  # Default to enabled if not set
    
    # Ensure all channels have a setting (even if not in config)
    for channel in ["console", "telegram", "webhook", "nats"]:
        setting_key = f"notify_channel_{channel}"
        if db.get_setting(setting_key) is None:
            # Default based on whether it's in config
            should_enable = channel in config.notify_channels
            db.set_setting(setting_key, "true" if should_enable else "false")
    
    notifier = Notifier(
        telegram_cfg=config.telegram,
        webhook_cfg=config.webhook,
        nats_cfg=config.nats,
        db=db,
    )

    # CHANGED: Pass, Tesla, and Apple pollers (but don't start them yet)
    google_poller = GooglePoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.google_cooldown_minutes,
        request_timeout=config.google_request_timeout,
    )
    
    tesla_poller = TeslaPoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.tesla_cooldown_minutes,
        request_timeout=config.tesla_request_timeout,
    )
    
    apple_poller = ApplePoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.apple_cooldown_minutes,
        request_timeout=config.apple_request_timeout,
    )
    
    microsoft_poller = MicrosoftPoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.microsoft_cooldown_minutes,
        request_timeout=config.microsoft_request_timeout,
    )
    
    orchestrator = PollOrchestrator(enabled_companies, config, db, http, notifier, google_poller, tesla_poller, apple_poller, microsoft_poller)

    # Start Google, Tesla, and Apple pollers based on their settings (already checked for validation above)
    if google_enabled:
        google_poller.start()
        logger.info("✓ Google Careers poller started")
    else:
        logger.info("✗ Google Careers poller disabled (can be enabled in settings)")
    
    if tesla_enabled:
        tesla_poller.start()
        logger.info("✓ Tesla Careers poller started")
    else:
        logger.info("✗ Tesla Careers poller disabled (can be enabled in settings)")
    
    if apple_enabled:
        apple_poller.start()
        logger.info("✓ Apple Careers poller started")
    else:
        logger.info("✗ Apple Careers poller disabled (can be enabled in settings)")
    
    if microsoft_enabled:
        microsoft_poller.start()
        logger.info("✓ Microsoft Careers poller started")
    else:
        logger.info("✗ Microsoft Careers poller disabled (can be enabled in settings)")

    try:
        orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        google_poller.stop()
        tesla_poller.stop()
        apple_poller.stop()
        microsoft_poller.stop()
        notifier.stop()
        listener.stop()  # Drain remaining logs from queue
        logger.info("Job Sniper stopped.")


if __name__ == "__main__":
    main()