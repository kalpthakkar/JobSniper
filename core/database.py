"""
core/database.py — SQLite-backed database for Job Sniper.

Tables:
- jobs: board_token, ats, hash, seen_ids (JSON), last_polled
- companies: board_token (PK), ats, priority
"""
import json
import sqlite3
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

from core.models import Company, ATSType, Priority


class JobDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._lock = threading.RLock()
        self._create_tables()
        self._migrate_old_data()

    def _create_tables(self):
        with self._lock:
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    board_token TEXT,
                    ats TEXT,
                    hash TEXT,
                    seen_ids TEXT,
                    last_polled TEXT,
                    metadata TEXT,
                    PRIMARY KEY (board_token, ats)
                )
            ''')
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS companies (
                    board_token TEXT PRIMARY KEY,
                    ats TEXT,
                    priority TEXT
                )
            ''')
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS notification_config (
                    id INTEGER PRIMARY KEY,
                    config_json TEXT
                )
            ''')
            self.conn.commit()
            self._ensure_metadata_column()

    def _ensure_metadata_column(self):
        self.cursor.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in self.cursor.fetchall()]
        if "metadata" not in cols:
            self.cursor.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT")
            self.conn.commit()

    def _migrate_old_data(self):
        # If old JSON exists, migrate jobs
        old_path = Path(self.db_path).parent / "job_db.json"
        if old_path.exists():
            with open(old_path, "r") as f:
                data = json.load(f)
            with self._lock:
                for key, value in data.items():
                    board_token, ats = key.split("__", 1)
                    seen_ids_json = json.dumps(value.get("seen_ids", []))
                    self.cursor.execute(
                        "INSERT OR IGNORE INTO jobs (board_token, ats, hash, seen_ids, last_polled, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                        (board_token, ats, value.get("hash", ""), seen_ids_json, value.get("last_polled", ""), json.dumps({}))
                    )
            self.conn.commit()
            # Remove legacy JSON file once data is migrated.
            try:
                old_path.unlink()
            except OSError:
                pass

    @staticmethod
    def compute_hash(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Public API for jobs
    # ------------------------------------------------------------------
    def get_record(self, board_token: str, ats: str) -> Optional[dict]:
        with self._lock:
            self.cursor.execute(
                "SELECT hash, seen_ids, last_polled, metadata FROM jobs WHERE board_token=? AND ats=?",
                (board_token, ats)
            )
            row = self.cursor.fetchone()
            if row:
                return {
                    "hash": row[0],
                    "seen_ids": json.loads(row[1]) if row[1] else [],
                    "last_polled": row[2],
                    "metadata": json.loads(row[3]) if len(row) > 3 and row[3] else {}
                }
        return None

    def has_changed(self, board_token: str, ats: str, new_hash: str) -> bool:
        record = self.get_record(board_token, ats)
        if record is None:
            return True
        return record.get("hash") != new_hash

    def update(self, board_token: str, ats: str, new_hash: str, all_ids: List[str], metadata: Optional[dict] = None):
        normalized_ids = sorted(set(all_ids))
        seen_ids_json = json.dumps(normalized_ids)
        last_polled = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self.get_record(board_token, ats)
            if metadata is None:
                metadata = existing.get("metadata", {}) if existing else {}
            metadata_json = json.dumps(metadata or {})
            self.cursor.execute(
                "INSERT OR REPLACE INTO jobs (board_token, ats, hash, seen_ids, last_polled, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (board_token, ats, new_hash, seen_ids_json, last_polled, metadata_json)
            )
            self.conn.commit()

    def stats(self) -> dict:
        with self._lock:
            self.cursor.execute("SELECT COUNT(*), SUM(json_array_length(seen_ids)) FROM jobs")
            row = self.cursor.fetchone()
            return {
                "total_tracked_companies": row[0] or 0,
                "total_seen_jobs": row[1] or 0
            }

    def get_notification_config(self) -> dict:
        with self._lock:
            self.cursor.execute(
                "SELECT config_json FROM notification_config WHERE id = 1"
            )
            row = self.cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return {}
        return {}

    def save_notification_config(self, config: dict):
        config_json = json.dumps(config)
        with self._lock:
            self.cursor.execute(
                "INSERT OR REPLACE INTO notification_config (id, config_json) VALUES (1, ?)",
                (config_json,)
            )
            self.conn.commit()

    # ------------------------------------------------------------------
    # Public API for companies
    # ------------------------------------------------------------------
    def get_companies(self) -> List[Company]:
        with self._lock:
            self.cursor.execute("SELECT board_token, ats, priority FROM companies")
            rows = self.cursor.fetchall()
            return [
                Company(
                    name=row[0],  # use board_token as name for now
                    board_token=row[0],
                    ats=ATSType(row[1]),
                    priority=Priority(row[2])
                ) for row in rows
            ]

    def add_company(self, board_token: str, ats: str, priority: str):
        with self._lock:
            self.cursor.execute(
                "INSERT OR IGNORE INTO companies (board_token, ats, priority) VALUES (?, ?, ?)",
                (board_token, ats, priority)
            )
            self.conn.commit()

    def update_company(self, board_token: str, ats: str, priority: str):
        with self._lock:
            self.cursor.execute(
                "UPDATE companies SET ats=?, priority=? WHERE board_token=?",
                (ats, priority, board_token)
            )
            self.conn.commit()

    def delete_company(self, board_token: str):
        with self._lock:
            self.cursor.execute("DELETE FROM companies WHERE board_token=?", (board_token,))
            self.conn.commit()
