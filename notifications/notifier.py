"""
notifications/notifier.py — Multi-channel notification dispatcher.

Supported channels:
  console  — Rich coloured terminal output (always available)
  telegram — Sends a Telegram message via Bot API
  webhook  — POSTs JSON payload to a configured URL
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

import requests

from core.models import Job
from core.description_parser import format_for_console, format_for_telegram, format_for_webhook
from core.filter import apply_all_filters

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
    def __init__(self, telegram_cfg: dict, webhook_cfg: dict, db: Optional[Any] = None):
        """
        Initialize notifier. Channels are now read from database, not passed in.
        
        Args:
            telegram_cfg: Telegram bot config (bot_token, chat_id)
            webhook_cfg: Webhook config (url, headers)
            db: JobDatabase instance for reading channel settings
        """
        self.telegram = telegram_cfg
        self.webhook = webhook_cfg
        self.db = db

    def _get_enabled_channels(self) -> List[str]:
        """Read currently enabled notification channels from database."""
        if self.db is None:
            return ["console"]  # Fallback to console only
        
        channels = []
        
        # Console is always available
        if self.db.get_setting("notify_channel_console") != "false":
            channels.append("console")
        
        # Telegram (only if configured)
        if (self.db.get_setting("notify_channel_telegram") != "false" and 
            self.telegram.get("bot_token") and self.telegram.get("chat_id")):
            channels.append("telegram")
        
        # Webhook (only if configured)
        if (self.db.get_setting("notify_channel_webhook") != "false" and
            self.webhook.get("url")):
            channels.append("webhook")
        
        return channels

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def notify(self, jobs: List[Job]):
        """Dispatch new jobs to all enabled channels (read from database)."""
        if not jobs:
            return

        # Apply all filters: notification rules + preferences
        filtered_jobs, notification_removed, preference_removed = apply_all_filters(jobs, self.db)
        if not filtered_jobs:
            logger.info(f"[notifier] All jobs filtered out (notification: {notification_removed}, preference: {preference_removed})")
            return

        # Get currently enabled channels from database
        channels = self._get_enabled_channels()
        
        for channel in channels:
            try:
                if channel == "console":
                    self._console(filtered_jobs)
                elif channel == "telegram":
                    self._telegram(filtered_jobs)
                elif channel == "webhook":
                    self._webhook(filtered_jobs)
                else:
                    logger.warning(f"Unknown notification channel: {channel}")
            except Exception as e:
                logger.error(f"Notification failed on channel '{channel}': {e}")

    # ------------------------------------------------------------------
    # Console (rich terminal output)
    # ------------------------------------------------------------------
    def _console(self, jobs: List[Job]):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        separator = "─" * 60

        # For large batches, show summary instead of individual jobs
        if len(jobs) > 50:
            print(f"\n{_GREEN}{_BOLD}{'🚨 BULK JOB ALERT':^60}{_RESET}")
            print(f"{_DIM}{separator}{_RESET}")
            print(f"\n  {_BOLD}{jobs[0].company}{_RESET}")
            print(f"  {_CYAN}{len(jobs)} new job opening(s) detected{_RESET}")
            
            # Group by location for summary
            locations = {}
            for job in jobs:
                loc = job.location or "Unknown"
                locations[loc] = locations.get(loc, 0) + 1
            
            print(f"\n  {_DIM}Location breakdown:{_RESET}")
            for loc in sorted(locations.keys()):
                count = locations[loc]
                print(f"    📍 {loc}: {count} job(s)")
        else:
            print(f"\n{_GREEN}{_BOLD}{'🚨 NEW JOB ALERT':^60}{_RESET}")
            print(f"{_DIM}{separator}{_RESET}")

            for job in jobs:
                remote_tag = f" {_CYAN}[REMOTE]{_RESET}" if job.remote else ""
                salary_tag = f"\n   {_YELLOW}💰 {job.salary}{_RESET}" if job.salary else ""
                dept_tag   = f"  •  {job.department}" if job.department else ""
                posted_tag = f"\n   {_DIM}Posted: {job.posted_at}{_RESET}" if job.posted_at else ""
                
                # Format description for console (truncated to 100 chars)
                description_text = ""
                if job.description:
                    truncated_desc = format_for_console(job.description)
                    if truncated_desc:
                        description_text = f"\n   {_DIM}📝 {truncated_desc}{_RESET}"

                print(f"\n  {_BOLD}{job.company}{_RESET}{dept_tag}")
                print(f"  {_GREEN}▶ {job.title}{_RESET}{remote_tag}")
                if job.location:
                    print(f"  📍 {job.location}")
                if salary_tag:
                    print(salary_tag)
                if description_text:
                    print(description_text)
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

        # For large job batches (>50), send batched summary messages instead of individual ones
        if len(jobs) > 50:
            logger.info(f"[telegram] Large batch detected ({len(jobs)} jobs) — sending batched summary")
            self._telegram_batch_summary(jobs, bot_token, chat_id)
        else:
            # For small batches, send individual job messages
            for job in jobs:
                self._telegram_single_job(job, bot_token, chat_id)
    
    def _telegram_single_job(self, job: Job, bot_token: str, chat_id: str):
        """Send a single job as a Telegram message."""
        remote_tag = " 🌍 Remote" if job.remote else ""
        salary_tag = f"\n💰 {job.salary}" if job.salary else ""
        dept_tag   = f"\n🏢 {job.department}" if job.department else ""
        loc_tag    = f"\n📍 {job.location}" if job.location else ""
        
        # Format description for Telegram (truncated to 100 chars, Markdown-escaped)
        description_tag = ""
        if job.description:
            truncated_desc = format_for_telegram(job.description)
            if truncated_desc:
                description_tag = f"\n📝 {truncated_desc}"

        text = (
            f"🚨 *New Job Alert*\n\n"
            f"*{self._escape(job.company)}*\n"
            f"➡️ {self._escape(job.title)}{remote_tag}\n"
            f"{loc_tag}{dept_tag}{salary_tag}{description_tag}\n\n"
            f"[Apply Now]({job.url})"
        )
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }, timeout=10)
        except Exception as e:
            logger.error(f"[telegram] Failed to send job alert: {e}")
    
    def _telegram_batch_summary(self, jobs: List[Job], bot_token: str, chat_id: str):
        """Send large job batches as a summary message (one message per 50 jobs)."""
        batch_size = 50
        
        for i in range(0, len(jobs), batch_size):
            batch = jobs[i:i+batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(jobs) + batch_size - 1) // batch_size
            
            # Build job list for this batch
            job_lines = []
            for j, job in enumerate(batch, 1):
                remote = "🌍" if job.remote else "📍"
                job_summary = (
                    f"{j}. *{self._escape(job.title[:40])}*\n"
                    f"   {remote} {self._escape(job.location[:30])}"
                )
                
                # Add truncated description if available
                if job.description:
                    truncated_desc = format_for_telegram(job.description)
                    if truncated_desc:
                        # Limit to 60 chars for batch summary
                        desc_preview = truncated_desc[:60] + "..." if len(truncated_desc) > 60 else truncated_desc
                        job_summary += f"\n   {desc_preview}"
                
                job_summary += f"\n   🔗 [View]({job.url})"
                job_lines.append(job_summary)
            
            text = (
                f"🚨 *New Jobs Alert* [{batch_num}/{total_batches}]\n\n"
                f"*{jobs[0].company}* — {len(batch)} new opening(s)\n\n"
                f"{chr(10).join(job_lines)}\n\n"
                f"📊 Total: {len(jobs)} new jobs this cycle"
            )
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            try:
                requests.post(url, json={
                    "chat_id":    chat_id,
                    "text":       text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,  # Disable preview to avoid rate limits
                }, timeout=10)
                logger.debug(f"[telegram] Sent batch {batch_num}/{total_batches} ({len(batch)} jobs)")
                # Small delay between batches to avoid Telegram rate limits
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"[telegram] Failed to send batch {batch_num}: {e}")

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
                    "description": format_for_webhook(j.description),  # Full description
                }
                for j in jobs
            ],
        }
        requests.post(url, json=payload, headers=headers, timeout=10)
