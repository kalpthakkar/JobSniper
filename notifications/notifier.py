"""
notifications/notifier.py — Multi-channel notification dispatcher.

Supported channels:
  console  — Rich coloured terminal output (always available)
  telegram — Sends a Telegram message via Bot API
  webhook  — POSTs JSON payload to a configured URL
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import requests

from core.models import Job

logger = logging.getLogger("job_sniper.notifier")

# ANSI colour codes for console output
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_RED    = "\033[91m"
_DIM    = "\033[2m"


class Notifier:
    def __init__(self, channels: List[str], telegram_cfg: dict, webhook_cfg: dict, db: Optional[Any] = None):
        self.channels    = channels
        self.telegram    = telegram_cfg
        self.webhook     = webhook_cfg
        self.db          = db

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def notify(self, jobs: List[Job]):
        """Dispatch new jobs to all configured channels."""
        if not jobs:
            return

        jobs = self._apply_notification_filters(jobs)
        if not jobs:
            logger.info("[notifier] No jobs matched notification filter configuration")
            return

        for channel in self.channels:
            try:
                if channel == "console":
                    self._console(jobs)
                elif channel == "telegram":
                    self._telegram(jobs)
                elif channel == "webhook":
                    self._webhook(jobs)
                else:
                    logger.warning(f"Unknown notification channel: {channel}")
            except Exception as e:
                logger.error(f"Notification failed on channel '{channel}': {e}")

    def _apply_notification_filters(self, jobs: List[Job]) -> List[Job]:
        config = {}
        if self.db is not None:
            config = self.db.get_notification_config() or {}

        if not config.get("enabled", False):
            return jobs

        def normalize(text: str, case_sensitive: bool) -> str:
            return text if case_sensitive else text.lower()

        def rule_matches(text: str, rule: dict) -> bool:
            value = str(rule.get("value", "")).strip()
            if not value:
                return False
            case_sensitive = bool(rule.get("case_sensitive", False))
            text = normalize(text or "", case_sensitive)
            pattern = normalize(value, case_sensitive)
            match_type = rule.get("match", "includes")
            if match_type == "starts_with":
                return text.startswith(pattern)
            if match_type == "ends_with":
                return text.endswith(pattern)
            return pattern in text

        def section_passes(text: str, section: dict) -> bool:
            if not section.get("enabled", False):
                return True
            rules = section.get("rules", []) or []
            if not rules:
                return True
            return any(rule_matches(text, rule) for rule in rules)

        blacklist = config.get("blacklist", {})
        filtered = []
        for job in jobs:
            if blacklist.get("enabled", False):
                if any(rule_matches(job.title if job.title else "", rule)
                       or rule_matches(job.company if job.company else "", rule)
                       or rule_matches(job.location if job.location else "", rule)
                       for rule in blacklist.get("rules", []) or []):
                    continue

            if not section_passes(job.title, config.get("job_title", {})):
                continue
            if not section_passes(job.company, config.get("company_name", {})):
                continue
            if not section_passes(job.location, config.get("location", {})):
                continue

            filtered.append(job)

        logger.info(f"[notifier] {len(filtered)}/{len(jobs)} jobs passed notification filters")
        return filtered

    # ------------------------------------------------------------------
    # Console (rich terminal output)
    # ------------------------------------------------------------------
    def _console(self, jobs: List[Job]):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        separator = "─" * 60

        print(f"\n{_GREEN}{_BOLD}{'🚨 NEW JOB ALERT':^60}{_RESET}")
        print(f"{_DIM}{separator}{_RESET}")

        for job in jobs:
            remote_tag = f" {_CYAN}[REMOTE]{_RESET}" if job.remote else ""
            salary_tag = f"\n   {_YELLOW}💰 {job.salary}{_RESET}" if job.salary else ""
            dept_tag   = f"  •  {job.department}" if job.department else ""
            posted_tag = f"\n   {_DIM}Posted: {job.posted_at}{_RESET}" if job.posted_at else ""

            print(f"\n  {_BOLD}{job.company}{_RESET}{dept_tag}")
            print(f"  {_GREEN}▶ {job.title}{_RESET}{remote_tag}")
            if job.location:
                print(f"  📍 {job.location}")
            if salary_tag:
                print(salary_tag)
            if posted_tag:
                print(posted_tag)
            print(f"  🔗 {_CYAN}{job.url}{_RESET}")

        print(f"\n{_DIM}{separator}")
        print(f"  Detected at {ts}  •  {len(jobs)} new opening(s){_RESET}\n")

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    def _telegram(self, jobs: List[Job]):
        bot_token = self.telegram.get("bot_token", "")
        chat_id   = self.telegram.get("chat_id", "")
        if not bot_token or not chat_id:
            logger.warning("Telegram not configured — skipping")
            return

        for job in jobs:
            remote_tag = " 🌍 Remote" if job.remote else ""
            salary_tag = f"\n💰 {job.salary}" if job.salary else ""
            dept_tag   = f"\n🏢 {job.department}" if job.department else ""
            loc_tag    = f"\n📍 {job.location}" if job.location else ""

            text = (
                f"🚨 *New Job Alert*\n\n"
                f"*{self._escape(job.company)}*\n"
                f"➡️ {self._escape(job.title)}{remote_tag}\n"
                f"{loc_tag}{dept_tag}{salary_tag}\n\n"
                f"[Apply Now]({job.url})"
            )
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }, timeout=10)

    @staticmethod
    def _escape(text: str) -> str:
        """Escape Telegram Markdown special chars."""
        for ch in r"_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------
    def _webhook(self, jobs: List[Job]):
        url     = self.webhook.get("url", "")
        headers = self.webhook.get("headers", {})
        if not url:
            logger.warning("Webhook URL not configured — skipping")
            return

        payload = {
            "event":     "new_jobs_detected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count":     len(jobs),
            "jobs": [
                {
                    "id":         j.id,
                    "title":      j.title,
                    "company":    j.company,
                    "location":   j.location,
                    "department": j.department,
                    "url":        j.url,
                    "posted_at":  j.posted_at,
                    "remote":     j.remote,
                    "salary":     j.salary,
                }
                for j in jobs
            ],
        }
        requests.post(url, json=payload, headers=headers, timeout=10)
