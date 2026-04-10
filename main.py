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
import sys
from pathlib import Path
from typing import List
import yaml

from core.config import Config
from core.database import JobDatabase
from core.models import Company
from core.http_client import HttpClient
from core.poller import PollOrchestrator
from core.google_poller import GooglePoller
from core.tesla_poller import TeslaPoller
from notifications.notifier import Notifier


# ─────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────
def setup_logging(level: str):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


# ─────────────────────────────────────────────────────────────
# CLI args
# ─────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Job Sniper — Real-time job posting monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                      Start full monitoring loop
  python main.py --config my.yaml     Use alternate config
  python main.py --company stripe     One-shot probe Stripe (debug)
  python main.py --list               Show all tracked companies
  python main.py --dashboard          Run web dashboard
        """,
    )
    parser.add_argument("--config",  default="config.yaml", help="Path to config file")
    parser.add_argument("--company", default=None,          help="One-shot probe a single board_token")
    parser.add_argument("--list",    action="store_true",   help="List all companies in database and exit")
    parser.add_argument("--dashboard", action="store_true", help="Run web dashboard for company management")
    return parser.parse_args()


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
    setup_logging(config.log_level)
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
        print(f"\nTracked companies ({len(companies)} total):\n")
        for c in companies:
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
    notifier = Notifier(
        channels=config.notify_channels,
        telegram_cfg=config.telegram,
        webhook_cfg=config.webhook,
        db=db,
    )

    orchestrator = PollOrchestrator(companies, config, db, http, notifier)

    # Start Google Careers poller
    google_poller = GooglePoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.google_cooldown_minutes,
        request_timeout=config.google_request_timeout,
    )
    google_poller.start()

    # Start Tesla Careers poller
    tesla_poller = TeslaPoller(
        db=db,
        notifier=notifier,
        cooldown_minutes=config.tesla_cooldown_minutes,
        request_timeout=config.tesla_request_timeout,
    )
    tesla_poller.start()

    try:
        orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        google_poller.stop()
        tesla_poller.stop()
        logger.info("Job Sniper stopped.")


if __name__ == "__main__":
    main()
