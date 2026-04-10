"""
ats/lever.py — Lever ATS adapter.

Public API:
  GET https://api.lever.co/v0/postings/{board_token}?mode=json&limit=50

Response shape: a JSON array (not an object):
[
  {
    "id": "uuid",
    "text": "Software Engineer",
    "categories": {
      "team": "Engineering",
      "location": "San Francisco, CA",
      "department": "Product"
    },
    "hostedUrl": "https://jobs.lever.co/company/uuid",
    "applyUrl": "https://jobs.lever.co/company/uuid/apply",
    "createdAt": 1700000000000   <- Unix ms timestamp
  }
]
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

import requests

from core.models import Job, Company
from core.http_client import HttpClient

logger = logging.getLogger("job_sniper.ats.lever")


def _is_posted_today(created_at_ms: int, disable_filter: bool = False) -> bool:
    """Check if job was created in the past 24 hours (Lever Unix milliseconds)."""
    if disable_filter:
        return True
    if not created_at_ms:
        return False
    try:
        dt = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt) < timedelta(hours=24)
    except (ValueError, TypeError, OSError, AttributeError):
        return False


def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    """
    Fetch all current open jobs from Lever.
    Returns: (raw_json_text, [job_id_str, ...])
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})
    timeout = schema.get("timeout")

    try:
        resp = http.get(url, params=params, timeout=timeout)
        data = resp.json()
        logger.debug(f"[Lever] {company.name} fetched {len(data)} jobs")
        # Filter to jobs created in past 24 hours
        today_jobs = [j for j in data if _is_posted_today(j.get("createdAt", 0))]
        ids = sorted({str(j.get("id", "")) for j in today_jobs})
        canonical = json.dumps(ids)
        return canonical, ids
    except requests.exceptions.RequestException as e:
        logger.error(f"[Lever] Failed to fetch {company.name}: {e}")
        raise


def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    """
    Fetch and return only jobs not already in seen_ids.
    Canonical function used by the poller.
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})
    timeout = schema.get("timeout")

    try:
        resp = http.get(url, params=params, timeout=timeout)
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[Lever] extract_new_jobs failed for {company.name}: {e}")
        return []

    new_jobs: List[Job] = []
    # Filter to jobs created in past 24 hours
    today_jobs = [j for j in data if _is_posted_today(j.get("createdAt", 0))]

    for raw in today_jobs:
        job_id = str(raw.get("id", ""))
        if job_id in seen_ids:
            continue

        categories = raw.get("categories", {})
        location   = categories.get("location", "")
        department = categories.get("team", "") or categories.get("department", "")
        remote     = "remote" in location.lower()

        # Lever createdAt is Unix milliseconds
        posted_at: Optional[str] = None
        created_ms = raw.get("createdAt")
        if created_ms:
            try:
                posted_at = datetime.fromtimestamp(
                    created_ms / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        new_jobs.append(Job(
            id=job_id,
            title=raw.get("text", "Untitled"),
            company=company.name,
            location=location,
            department=department,
            url=raw.get("hostedUrl", raw.get("applyUrl", "")),
            posted_at=posted_at,
            remote=remote,
            salary=None,
            raw=raw,
        ))

    return new_jobs
