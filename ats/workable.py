"""
ats/workable.py — Workable ATS adapter.

Public API:
  GET https://apply.workable.com/api/v1/widget/accounts/{board_token}

Response shape (abbreviated):
{
  "jobs": [
    {
      "shortcode": "ABC123",
      "title": "Software Engineer",
      "department": "Engineering",
      "location": {
        "city": "Amsterdam",
        "country": "Netherlands",
        "remote": false
      },
      "url": "https://apply.workable.com/company/j/ABC123/",
      "created_at": "2024-04-01T10:00:00Z"
    }
  ]
}
"""
import json
import logging
from datetime import datetime, timedelta, date
from typing import List, Tuple

import requests

from core.models import Job, Company
from core.http_client import HttpClient

logger = logging.getLogger("job_sniper.ats.workable")


def _is_posted_today(published_on_str: str, disable_filter: bool = False) -> bool:
    """Check if job was published in the past 24 hours (Workable date-only format YYYY-MM-DD)."""
    if disable_filter:
        return True
    if not published_on_str:
        return False
    try:
        job_date = datetime.strptime(published_on_str, "%Y-%m-%d").date()
        now_date = date.today()
        return (now_date - job_date).days < 1
    except (ValueError, TypeError, AttributeError):
        return False


def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    """
    Fetch all current open jobs from Workable.
    Returns: (raw_json_text, [job_id_str, ...])
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})

    try:
        resp = http.get(url, params=params)
        data = resp.json()
        jobs = data.get("jobs", [])
        # Filter to jobs published in past 24 hours (or all if filter disabled)
        today_jobs = [j for j in jobs if _is_posted_today(j.get("published_on", ""), disable_filter=disable_filter)]
        ids = sorted({str(j.get("shortcode", j.get("id", ""))) for j in today_jobs})
        canonical = json.dumps(ids)
        return canonical, ids
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workable] Failed to fetch {company.name}: {e}")
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

    try:
        resp = http.get(url, params=params)
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workable] extract_new_jobs failed for {company.name}: {e}")
        return []

    raw_jobs = data.get("jobs", [])
    # Filter to jobs published in past 24 hours (or all if filter disabled)
    today_jobs = [j for j in raw_jobs if _is_posted_today(j.get("published_on", ""), disable_filter=disable_filter)]
    new_jobs: List[Job] = []

    for raw in today_jobs:
        job_id = str(raw.get("shortcode", raw.get("id", "")))
        if job_id in seen_ids:
            continue

        loc_obj = raw.get("location", {})
        city    = loc_obj.get("city", "")
        country = loc_obj.get("country", "")
        remote  = loc_obj.get("remote", False)
        location = f"{city}, {country}".strip(", ") if (city or country) else ""
        if remote and not location:
            location = "Remote"

        new_jobs.append(Job(
            id=job_id,
            title=raw.get("title", "Untitled"),
            company=company.name,
            location=location,
            department=raw.get("department", ""),
            url=raw.get("url", ""),
            posted_at=raw.get("created_at"),
            remote=remote,
            salary=None,
            raw=raw,
        ))

    return new_jobs
