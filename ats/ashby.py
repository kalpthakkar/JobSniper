"""
ats/ashby.py — Ashby ATS adapter.

FIX HISTORY:
  v1: Used api.ashbyhq.com/posting-api/ → empty body on 200 for most companies.
  v2: Added GQL primary, but queried fields that don't exist on the
      JobPostingBriefsWithIdsAndTeamId type: isRemote, publishedDate, jobUrl.
      Ashby's schema has these fields at a different level.
  v3 (current):
      - GQL query only requests fields confirmed to exist on that type:
        id, title, teamId, locationName, employmentType, compensationTierSummary
      - isRemote, publishedAt, jobUrl, applyUrl come from the REST endpoint,
        which returns the full shape confirmed by real payloads.
      - Strategy: GQL for fast ID-set diffing (minimal fields, low bandwidth),
        REST for full job detail when a new ID is detected.

STRATEGY:
  FETCH  (hash check)  → GQL  (minimal fields, just needs id + title + location)
                          fallback: REST full payload
  ENRICH (new jobs)    → REST (full payload with isRemote, publishedAt, jobUrl,
                               applyUrl, compensation, department, team)
                          fallback: build from GQL fields we do have

Real REST payload shape (confirmed from live Ramp data):
{
  "jobs": [{
    "id": "uuid",
    "title": "...",
    "department": "Product",
    "team": "Product Operations",
    "employmentType": "FullTime",
    "location": "New York, NY (HQ)",
    "publishedAt": "2026-03-17T20:30:40.304+00:00",
    "isRemote": true,
    "workplaceType": "Hybrid",
    "jobUrl": "https://jobs.ashbyhq.com/ramp/uuid",
    "applyUrl": "https://jobs.ashbyhq.com/ramp/uuid/application",
    "compensation": {
      "compensationTierSummary": "$150K - $250K  Offers Equity",
      ...
    }
  }]
}

GQL type JobPostingBriefsWithIdsAndTeamId confirmed fields:
  id, title, teamId, locationName, employmentType, compensationTierSummary
  (isRemote / publishedDate / jobUrl do NOT exist on this GQL type)
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

import requests

from core.models import Job, Company
from core.http_client import HttpClient
from core.description_parser import parse_html_description

logger = logging.getLogger("job_sniper.ats.ashby")


def _is_posted_today(published_at_str: str, disable_filter: bool = False) -> bool:
    """Check if job was published in the past 24 hours (Ashby ISO 8601 format)."""
    if disable_filter:
        return True
    if not published_at_str:
        return False
    try:
        dt = datetime.fromisoformat(published_at_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - dt) < timedelta(hours=24)
    except (ValueError, TypeError, AttributeError):
        return False


class RateLimitError(Exception):
    """Raised when Ashby returns 429 (Too Many Requests)."""
    pass


# ── GraphQL query — ONLY fields confirmed on JobPostingBriefsWithIdsAndTeamId ─
# Removed: isRemote, publishedDate, jobUrl  (not on this GQL type)
_GQL_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    teams {
      id
      name
      parentTeamId
      __typename
    }
    jobPostings {
      id
      title
      teamId
      locationName
      employmentType
      compensationTierSummary
      __typename
    }
    __typename
  }
}
""".strip()

_GQL_URL  = "https://jobs.ashbyhq.com/api/non-user-graphql"
_REST_URL = "https://api.ashbyhq.com/posting-api/job-board/{board_token}"


# ── Internal helpers ──────────────────────────────────────────────────────────

_ASHBY_GQL_MAX_RPS = 2
_ASHBY_GQL_MIN_INTERVAL = 1.0 / _ASHBY_GQL_MAX_RPS
_gql_lock = threading.Lock()
_gql_last_call_at = 0.0


def _gql_throttle() -> None:
    global _gql_last_call_at
    with _gql_lock:
        now = time.monotonic()
        wait = _ASHBY_GQL_MIN_INTERVAL - (now - _gql_last_call_at)
        if wait > 0:
            time.sleep(wait)
        _gql_last_call_at = time.monotonic()


def _gql_fetch(company: Company, http: HttpClient) -> Optional[dict]:
    """
    Call Ashby's internal GraphQL endpoint with a minimal field set
    that is confirmed to exist on JobPostingBriefsWithIdsAndTeamId.
    Returns parsed data dict or None on any failure.
    Raises RateLimitError on 429 (Too Many Requests) or ReadTimeout (overloaded API).
    """
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": company.board_token},
        "query": _GQL_QUERY,
    }
    _gql_throttle()
    try:
        resp = http.post(_GQL_URL, json_body=payload)
        body = resp.text.strip()
        if not body:
            logger.warning(f"[Ashby/GQL] Empty body for {company.name}")
            return None
        data = json.loads(body)
        if "errors" in data:
            # Log just the messages, not the full location objects
            msgs = [e.get("message", str(e)) for e in data["errors"]]
            logger.warning(f"[Ashby/GQL] GraphQL errors for {company.name}: {msgs}")
            return None
        if not data.get("data", {}).get("jobBoard"):
            logger.warning(f"[Ashby/GQL] Null jobBoard for {company.name} — board_token may be wrong")
            return None
        return data
    except requests.exceptions.ReadTimeout as e:
        # Treat timeout as a rate limit signal
        raise RateLimitError(f"Ashby GQL read timeout (overloaded) for {company.name}: {e}")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            raise RateLimitError(f"Ashby GQL rate limit (429) for {company.name}")
        logger.warning(f"[Ashby/GQL] HTTP error for {company.name}: {e}")
        return None
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        logger.warning(f"[Ashby/GQL] Failed for {company.name}: {e}")
        return None


def _rest_fetch(company: Company, http: HttpClient) -> Optional[dict]:
    """
    REST posting API — full payload including isRemote, publishedAt, jobUrl.
    Guards against empty / non-JSON body.
    Raises RateLimitError on 429 (Too Many Requests) or ReadTimeout (overloaded API).
    """
    url = _REST_URL.format(board_token=company.board_token)
    try:
        resp = http.get(url, params={"includeCompensation": "true"})
        body = resp.text.strip()
        if not body:
            logger.warning(
                f"[Ashby/REST] Empty body for {company.name} (status={resp.status_code}) "
                "— verify board_token at https://jobs.ashbyhq.com/<board_token>"
            )
            return None
        if not body.startswith("{"):
            logger.warning(f"[Ashby/REST] Non-JSON body for {company.name}: {body[:100]!r}")
            return None
        return json.loads(body)
    except requests.exceptions.ReadTimeout as e:
        # Treat timeout as a rate limit signal
        raise RateLimitError(f"Ashby REST read timeout (overloaded) for {company.name}: {e}")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            raise RateLimitError(f"Ashby REST rate limit (429) for {company.name}")
        logger.warning(f"[Ashby/REST] HTTP error for {company.name}: {e}")
        return None
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        logger.warning(f"[Ashby/REST] Failed for {company.name}: {e}")
        return None


def _parse_gql_jobs(data: dict) -> List[dict]:
    try:
        return data["data"]["jobBoard"]["jobPostings"] or []
    except (KeyError, TypeError):
        return []


def _parse_rest_jobs(data: dict) -> List[dict]:
    return data.get("jobs", [])


def _job_from_rest(raw: dict, company: Company) -> Job:
    """
    Build a Job from the REST payload — has full field set.
    REST uses: location (not locationName), team (not teamName),
    publishedAt (not publishedDate), jobUrl + applyUrl.
    compensation is a nested object.
    
    Description handling:
      - Prefer descriptionPlain (already plain text)
      - Fall back to descriptionHtml if descriptionPlain is empty/missing
      - Parse HTML if using descriptionHtml
    """
    location  = raw.get("location", "") or raw.get("locationName", "") or ""
    is_remote = raw.get("isRemote", False)
    if is_remote and not location:
        location = "Remote"
    elif raw.get("workplaceType") == "Remote":
        is_remote = True

    # Salary: REST nests it under compensation.compensationTierSummary
    comp = raw.get("compensation") or {}
    salary: Optional[str] = (
        comp.get("compensationTierSummary")
        or raw.get("compensationTierSummary")
    )

    # Department: REST has both "department" and "team" as flat strings
    department = raw.get("team", "") or raw.get("department", "")

    # URL: prefer jobUrl (public listing), fall back to applyUrl
    url = raw.get("jobUrl", "") or raw.get("applyUrl", "")

    # Description: prefer descriptionPlain, fall back to descriptionHtml
    description: Optional[str] = None
    description_plain = raw.get("descriptionPlain", "")
    if description_plain and description_plain.strip():
        # Use plain text directly (already in plain text format)
        description = description_plain.strip()
    else:
        # Fall back to HTML version if plain text is empty
        description_html = raw.get("descriptionHtml", "")
        if description_html and description_html.strip():
            # Parse HTML to plain text
            description = parse_html_description(description_html)

    return Job(
        id=str(raw.get("id", "")),
        title=raw.get("title", "Untitled"),
        company=company.name,
        location=location,
        department=department,
        url=url,
        posted_at=raw.get("publishedAt"),   # "2026-03-17T20:30:40.304+00:00"
        remote=is_remote,
        salary=salary,
        description=description,
        raw=raw,
    )


def _job_from_gql(raw: dict, company: Company, teams: dict) -> Job:
    """
    Fallback: build a Job from GQL fields only (fewer fields available).
    Used when REST is unavailable but GQL succeeded.
    
    Description handling:
      - Prefer descriptionPlain if available
      - Fall back to descriptionHtml if present
      - May be None if not available in GQL response
    """
    location = raw.get("locationName", "") or ""
    
    # Description: prefer descriptionPlain, fall back to descriptionHtml
    description: Optional[str] = None
    description_plain = raw.get("descriptionPlain", "")
    if description_plain and description_plain.strip():
        description = description_plain.strip()
    else:
        description_html = raw.get("descriptionHtml", "")
        if description_html and description_html.strip():
            description = parse_html_description(description_html)
    
    return Job(
        id=str(raw.get("id", "")),
        title=raw.get("title", "Untitled"),
        company=company.name,
        location=location,
        department=teams.get(raw.get("teamId", ""), ""),
        url=f"https://jobs.ashbyhq.com/{company.board_token}/{raw.get('id', '')}",
        posted_at=None,   # not available in this GQL query
        remote=False,     # not available in this GQL query
        salary=raw.get("compensationTierSummary"),
        description=description,
        raw=raw,
    )


# ── Public adapter contract ───────────────────────────────────────────────────

def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    """
    Fetch all current open jobs from Ashby for hash-change detection.
    Uses GQL (minimal, fast) for the ID set; falls back to REST.
    Returns: (canonical_text_for_hashing, [job_id_str, ...])
    Raises RuntimeError if both strategies fail (except RateLimitError).
    Raises RateLimitError if rate-limited; caller should apply global backoff.
    
    Args:
        disable_filter: If True, include all jobs regardless of publish date.
    """
    rate_limit_error = None

    # 1. Try REST — full payload. This is the most stable endpoint and avoids
    #    the shared Ashby GraphQL rate limit hotspot for fetch/hash checks.
    try:
        rest_data = _rest_fetch(company, http)
        if rest_data is not None:
            raw_jobs  = _parse_rest_jobs(rest_data)
            # Filter to jobs published in past 24 hours (or all if filter disabled)
            today_jobs = [j for j in raw_jobs if _is_posted_today(j.get("publishedAt", ""), disable_filter=disable_filter)]
            ids       = sorted({str(j.get("id", "")) for j in today_jobs})
            canonical = json.dumps(ids)
            logger.debug(f"[Ashby/REST] {company.name}: {len(ids)} jobs")
            return canonical, ids
    except RateLimitError as e:
        rate_limit_error = e

    # 2. Fallback: GQL only if REST failed entirely.
    try:
        gql_data = _gql_fetch(company, http)
        if gql_data is not None:
            raw_jobs  = _parse_gql_jobs(gql_data)
            ids       = sorted({str(j["id"]) for j in raw_jobs})
            canonical = json.dumps(ids)
            logger.debug(f"[Ashby/GQL] {company.name}: {len(ids)} jobs")
            return canonical, ids
    except RateLimitError as e:
        if rate_limit_error is None:
            rate_limit_error = e

    # If we caught a rate limit error, propagate it (caller will apply global backoff)
    if rate_limit_error is not None:
        raise rate_limit_error

    raise RuntimeError(
        f"Both GQL and REST failed for {company.name} (board_token={company.board_token!r}). "
        "Verify the slug at: https://jobs.ashbyhq.com/<board_token>"
    )


def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    """
    Called only when a hash mismatch is detected.
    Fetches full REST payload for rich job data; GQL is fallback.
    Canonical function — same name across all ATS adapters.
    
    Args:
        disable_filter: If True, include all jobs regardless of publish date.
    """
    # PRIMARY: REST gives us isRemote, publishedAt, jobUrl, salary, team
    rest_data = _rest_fetch(company, http)
    if rest_data is not None:
        raw_jobs = _parse_rest_jobs(rest_data)
        # Filter to jobs published in past 24 hours (or all if filter disabled)
        today_jobs = [j for j in raw_jobs if _is_posted_today(j.get("publishedAt", ""), disable_filter=disable_filter)]
        return [
            _job_from_rest(raw, company)
            for raw in today_jobs
            if str(raw.get("id", "")) not in seen_ids
        ]

    # FALLBACK: GQL — fewer fields but still usable
    gql_data = _gql_fetch(company, http)
    if gql_data is not None:
        raw_jobs = _parse_gql_jobs(gql_data)
        teams    = {
            t["id"]: t["name"]
            for t in gql_data["data"]["jobBoard"].get("teams", [])
        }
        return [
            _job_from_gql(raw, company, teams)
            for raw in raw_jobs
            if str(raw.get("id", "")) not in seen_ids
        ]

    logger.error(
        f"[Ashby] extract_new_jobs: both strategies failed for {company.name}. "
        f"Verify board_token={company.board_token!r} at https://jobs.ashbyhq.com/<board_token>"
    )
    return []
