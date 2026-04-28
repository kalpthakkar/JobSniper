"""
core/config.py — Load and validate config.yaml
"""
import yaml
from pathlib import Path
from typing import List, Dict, Any

from core.models import Company, ATSType, Priority


class Config:
    def __init__(self, path: str = "config.yaml"):
        raw = yaml.safe_load(Path(path).read_text())

        self.system: Dict[str, Any]   = raw["system"]
        self.ats_schemas: Dict        = raw["ats_schemas"]

        # Convenience shortcuts
        self.max_workers: int           = self.system.get("max_workers", 20)
        self.db_path: str               = self.system.get("db_path", "data/job_db.db")  # Changed to .db
        self.request_timeout: int       = self.system.get("request_timeout", 30)
        self.max_retries: int           = self.system.get("max_retries", 3)
        self.retry_delay: int           = self.system.get("retry_delay", 2)
        self.ip_strategy: str           = self.system.get("ip_strategy", "user_agent_rotation")
        self.proxy_file: str            = self.system.get("proxy_file", "")
        self.notify_channels: List[str] = self.system.get("notify_channels", ["console"])
        self.telegram: Dict             = self.system.get("telegram", {})
        self.webhook: Dict              = self.system.get("webhook", {})
        self.log_level: str             = self.system.get("log_level", "INFO")

        # Google settings
        self.google_enabled: bool       = self.system.get("google_enabled", True)
        self.google_cooldown_minutes: int = self.system.get("google_cooldown_minutes", 3)
        self.google_request_timeout: int = self.system.get("google_request_timeout", 30)

        # Tesla settings
        self.tesla_enabled: bool        = self.system.get("tesla_enabled", True)
        self.tesla_cooldown_minutes: int = self.system.get("tesla_cooldown_minutes", 3)
        self.tesla_request_timeout: int = self.system.get("tesla_request_timeout", 60)

        # Apple settings
        self.apple_enabled: bool        = self.system.get("apple_enabled", True)
        self.apple_cooldown_minutes: int = self.system.get("apple_cooldown_minutes", 3)
        self.apple_request_timeout: int = self.system.get("apple_request_timeout", 10)

        # Microsoft settings
        self.microsoft_enabled: bool        = self.system.get("microsoft_enabled", True)
        self.microsoft_cooldown_minutes: int = self.system.get("microsoft_cooldown_minutes", 3)
        self.microsoft_request_timeout: int = self.system.get("microsoft_request_timeout", 10)

        # 24-hour filter settings per ATS type
        disable_24h_filter_config = self.system.get("disable_24h_filter", {})
        self.disable_24h_filter: Dict[str, bool] = {
            "greenhouse": disable_24h_filter_config.get("greenhouse", False),
            "lever": disable_24h_filter_config.get("lever", False),
            "ashby": disable_24h_filter_config.get("ashby", False),
            "workable": disable_24h_filter_config.get("workable", False),
            "workday": disable_24h_filter_config.get("workday", False),
        }

        # Frequency inspection settings
        self.frequency_inspection_enabled: bool = self.system.get("frequency_inspection_enabled", False)
        self.frequency_inspect_board_token: str = self.system.get("frequency_inspect_board_token", "")

    def get_ats_schema(self, ats: ATSType) -> Dict:
        return self.ats_schemas.get(ats.value, {})
