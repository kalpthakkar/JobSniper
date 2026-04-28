"""
ats/greenhouse.py — Greenhouse ATS adapter.

Public API:
  GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

Response shape (abbreviated):
{
  "jobs": [
    {
      "id": 12345,
      "title": "Software Engineer",
      "location": {"name": "San Francisco, CA"},
      "departments": [{"name": "Engineering"}],
      "absolute_url": "https://boards.greenhouse.io/...",
      "updated_at": "2024-04-01T10:00:00.000Z",
      "metadata": [...],
      "content": "<p>Job description HTML...</p>"
    }
  ]
}
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

import requests

from core.models import Job, Company
from core.http_client import HttpClient
from core.description_parser import parse_html_description

logger = logging.getLogger("job_sniper.ats.greenhouse")


def _is_posted_today(timestamp_str: str, disable_filter: bool = False) -> bool:
    """
    Check if job was published in the past 24 hours (Greenhouse ISO 8601 format).
    
    Args:
        timestamp_str: ISO 8601 timestamp string
        disable_filter: If True, accept all jobs (ignore 24h window). Useful for baseline setup.
    """
    if disable_filter:
        # Accept all jobs when filter is disabled
        return True
    
    if not timestamp_str:
        return False
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - dt) < timedelta(hours=24)
    except (ValueError, TypeError, AttributeError):
        return False


# -------------------------------------------------------------------
# Fetch — returns (raw_response_text, list_of_all_job_ids)
# -------------------------------------------------------------------
def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    """
    Fetch all current open jobs from Greenhouse.
    Returns: (raw_json_text, [job_id_str, ...])
    
    Args:
        disable_filter: If True, include all jobs regardless of publish date.
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})

    try:
        resp = http.get(url, params=params)
        data = resp.json()
        jobs = data.get("jobs", [])
        # Filter to jobs posted in past 24 hours (or all if filter disabled)
        today_jobs = [j for j in jobs if _is_posted_today(j.get("first_published", ""), disable_filter=disable_filter)]
        ids = sorted({str(j["id"]) for j in today_jobs})
        canonical = json.dumps(ids)
        return canonical, ids
    except requests.exceptions.RequestException as e:
        logger.error(f"[Greenhouse] Failed to fetch {company.name}: {e}")
        raise


# -------------------------------------------------------------------
# extract_new_jobs — called only when hash mismatch detected
# Returns list of Job objects that are new (not in seen_ids)
# -------------------------------------------------------------------
def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    """
    Re-fetches jobs and returns only the ones whose IDs aren't in seen_ids.
    This is the canonical function name used by the poller to get new openings.
    
    Args:
        disable_filter: If True, include all jobs regardless of publish date.
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})

    try:
        resp = http.get(url, params=params)
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[Greenhouse] extract_new_jobs failed for {company.name}: {e}")
        return []

    raw_jobs = data.get("jobs", [])
    # Filter to jobs posted in past 24 hours (or all if filter disabled)
    today_jobs = [j for j in raw_jobs if _is_posted_today(j.get("first_published", ""), disable_filter=disable_filter)]
    new_jobs: List[Job] = []

    for raw in today_jobs:
        job_id = str(raw.get("id", ""))
        if job_id in seen_ids:
            continue

        # Parse location
        location_obj = raw.get("location", {})
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else ""

        # Parse department (list)
        departments = raw.get("departments", [])
        department = departments[0]["name"] if departments else ""

        # Salary is rarely in Greenhouse public API, mark as None
        salary: Optional[str] = None

        # Parse job description from HTML content
        raw_html_content = raw.get("content", "")
        description = parse_html_description(raw_html_content)

        new_jobs.append(Job(
            id=job_id,
            title=raw.get("title", "Untitled"),
            company=company.name,
            location=location,
            department=department,
            url=raw.get("absolute_url", ""),
            posted_at=raw.get("updated_at"),
            remote="remote" in location.lower(),
            salary=salary,
            description=description,
            raw=raw,
        ))

    return new_jobs
