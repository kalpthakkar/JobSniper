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
      "commitment": "Part-Time",
      "allLocations": [
        "San Francisco, CA"
      ]
    },
    "hostedUrl": "https://jobs.lever.co/company/uuid",
    "applyUrl": "https://jobs.lever.co/company/uuid/apply",
    "createdAt": 1700000000000,   <- Unix ms timestamp
    "descriptionPlain": "About Company...",
    "description": "\\u003Cp\\u003EAbout Company...\\u003C/p\\u003E",
    "list": [
      {"text": "What You'll Do:", "content": "\\u003Cdiv\\u003E\\u003Cli\\u003E...\\u003C/li\\u003E\\u003C/div\\u003E"},
      {"text": "What You'll Bring:", "content": "\\u003Cdiv\\u003E\\u003Cli\\u003E...\\u003C/li\\u003E\\u003C/div\\u003E"}
    ],
    "salaryRange": {
      "min": 15,
      "max": 15,
      "currency": "USD",
      "interval": "per-hour-wage"
    },
    "country": "US",
    "workplaceType": "onsite",
    "additionalPlain": "Closing text...",
    "additional": "\\u003Cp\\u003EClosing text...\\u003C/p\\u003E"
  }
]
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional, Dict, Any

import requests

from core.models import Job, Company
from core.http_client import HttpClient
from core.description_parser import parse_html_description

logger = logging.getLogger("job_sniper.ats.lever")

# ─────────────────────────────────────────────────────────────────────
# Description parsing helpers
# ─────────────────────────────────────────────────────────────────────

def _decode_unicode_escaped_html(encoded_str: Optional[str]) -> Optional[str]:
    """
    Decode Unicode escape sequences in a string.
    
    Example: "\\u003Cp\\u003EHello\\u003C/p\\u003E" → "<p>Hello</p>"
    
    Used to decode Lever's HTML descriptions which come as Unicode-escaped strings.
    """
    if not encoded_str or not isinstance(encoded_str, str):
        return None
    
    # Strip whitespace for checking
    if not encoded_str.strip():
        return None
    
    try:
        # Use json.loads to decode Unicode escape sequences
        # Wrap in quotes to create valid JSON string
        decoded = json.loads(f'"{encoded_str}"')
        return decoded if (decoded and decoded.strip()) else None
    except (json.JSONDecodeError, ValueError):
        # If decoding fails, return original string if not empty
        return encoded_str.strip() if encoded_str.strip() else None


def _build_lever_description(raw_job: Dict[str, Any]) -> Optional[str]:
    """
    Build complete job description from Lever's multi-part structure.
    
    Structure:
    1. Intro: descriptionPlain (preferred) or description (Unicode-escaped HTML, fallback)
    2. Core: list[{text: "Section Title", content: "Unicode-escaped HTML"}, ...]
    3. Closing: additionalPlain (preferred) or additional (Unicode-escaped HTML, fallback)
    
    Returns assembled plain text description or None if no description available.
    """
    parts = []
    
    # Part 1: Introduction
    intro = None
    description_plain = raw_job.get("descriptionPlain", "")
    if description_plain and description_plain.strip():
        intro = description_plain.strip()
    else:
        # Fallback to encoded HTML
        description_html = raw_job.get("description", "")
        if description_html:
            decoded = _decode_unicode_escaped_html(description_html)
            if decoded:
                intro = parse_html_description(decoded)
    
    if intro:
        parts.append(intro)
    
    # Part 2: Core content (list of sections)
    job_list = raw_job.get("list", [])
    if job_list and isinstance(job_list, list):
        for section in job_list:
            if not isinstance(section, dict):
                continue
            
            section_title = section.get("text", "").strip()
            section_content = section.get("content", "")
            
            if section_content:
                # Decode Unicode escape sequences
                decoded_html = _decode_unicode_escaped_html(section_content)
                if decoded_html:
                    # Parse HTML to plain text
                    parsed_content = parse_html_description(decoded_html)
                    if parsed_content:
                        if section_title:
                            parts.append(f"\n{section_title}\n{parsed_content}")
                        else:
                            parts.append(f"\n{parsed_content}")
    
    # Part 3: Closing/Additional info
    additional = None
    additional_plain = raw_job.get("additionalPlain", "")
    if additional_plain and additional_plain.strip():
        additional = additional_plain.strip()
    else:
        # Fallback to encoded HTML
        additional_html = raw_job.get("additional", "")
        if additional_html:
            decoded = _decode_unicode_escaped_html(additional_html)
            if decoded:
                additional = parse_html_description(decoded)
    
    if additional:
        parts.append(f"\n{additional}")
    
    # Assemble final description
    if parts:
        return "\n".join(parts)
    
    return None


# ─────────────────────────────────────────────────────────────────────
# Rate limiting parameters for Lever
# ─────────────────────────────────────────────────────────────────────
REQUEST_DELAY = 0.3  # Delay between successive Lever requests (seconds)


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
    
    Treats ReadTimeoutError as a rate limit signal — allows caller to apply backoff.
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
    except requests.exceptions.ReadTimeout as e:
        # Treat timeout as a rate limit signal
        # Re-raise as RateLimitError so scheduler applies exponential backoff
        from ats.ashby import RateLimitError
        logger.warning(f"[Lever] {company.name} read timeout (treating as rate limit): {e}")
        raise RateLimitError(f"Read timeout on Lever API: {e}") from e
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
    
    Adds inter-request delays to prevent hammering Lever API.
    Treats ReadTimeoutError as a rate limit signal.
    """
    url = schema["base_url"].format(board_token=company.board_token)
    params = schema.get("params", {})
    timeout = schema.get("timeout")

    # Add delay before making request to avoid hammering Lever
    time.sleep(REQUEST_DELAY)

    try:
        resp = http.get(url, params=params, timeout=timeout)
        data = resp.json()
    except requests.exceptions.ReadTimeout as e:
        # Treat timeout as a rate limit signal
        from ats.ashby import RateLimitError
        logger.warning(f"[Lever] {company.name} read timeout in extract_new_jobs (treating as rate limit): {e}")
        raise RateLimitError(f"Read timeout on Lever API: {e}") from e
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
        country_descriptor = raw.get("country")
        if country_descriptor:
            location += f' • {country_descriptor}'
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
            description=_build_lever_description(raw),
            raw=raw,
        ))

    return new_jobs
