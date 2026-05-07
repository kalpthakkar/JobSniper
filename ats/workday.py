"""
ats/workday.py — Workday ATS adapter (performance-optimized).

KEY OPTIMIZATIONS vs previous version:
────────────────────────────────────────────────────────────────────────────
1. SINGLE PAGE FETCH PER POLL CYCLE
   Old: fetch() called _fetch_workday_page(), then extract_new_jobs() called
        _fetch_workday_page() AGAIN = 2 full page-fetches per company update.
   New: fetch() returns a cache bundle (token/tenant/site/origin/headers)
        alongside the IDs. extract_new_jobs() accepts this bundle and skips
        the redundant page fetch entirely.

2. RETRY BACKOFF SLASHED
   Old: MAX_RETRIES=3, INITIAL_BACKOFF=2s → worst-case 14s of sleep inside
        one worker thread on a single company.
   New: MAX_RETRIES=2, INITIAL_BACKOFF=0.5s → worst-case 1s of sleep.
        At schema timeout=10s, worst case per company = 20s+1s = 21s vs 141s.

3. PAGINATION_DELAY ELIMINATED
   Old: 1.0s sleep between every pagination page, INSIDE the worker thread.
   New: No fixed sleep between pages. Workday's own response latency (~200ms)
        is sufficient natural spacing. We've never seen Workday 429 on pagination.

4. NO NESTED THREADPOOLEXECUTOR
   Old: extract_new_jobs() spawned a ThreadPoolExecutor(max_workers=5) INSIDE
        each of the 20 worker threads — up to 100 extra threads system-wide,
        competing for connections and CPU.
   New: Detail fetches are sequential within the worker. The per-company
        worker IS the thread; sequential fetches reuse the shared HTTP session.
        For Workday, there are rarely >3 new jobs per company per poll anyway.

5. GLOBAL SEMAPHORE REMOVED
   Old: _detail_fetch_semaphore = Semaphore(5) serialized ALL detail fetches
        across all workers, meaning 20 workers couldn't fetch details concurrently.
   New: No global semaphore. Workers run independently. The shared HTTP session's
        connection pool (pool_maxsize=200) is the natural concurrency limit.

6. WORKDAY-SPECIFIC TIMEOUT IN CONFIG
   Old: Schema timeout=45s was applied to every request including the page fetch.
   New: Page fetch uses a shorter timeout (configurable via schema["page_timeout"]);
        API calls use schema["timeout"]. Defaults: page=10s, api=15s.

7. REDIRECT FETCH REUSES CONNECTION
   Old: redirect fetch called http.get() with fresh headers = new TCP connection.
   New: redirect re-uses the same session via the same http.get() path.
────────────────────────────────────────────────────────────────────────────
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.http_client import HttpClient
from core.models import Company, Job
from core.description_parser import parse_html_description

logger = logging.getLogger("job_sniper.ats.workday")

REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Retry config — tight to avoid worker starvation
_MAX_RETRIES    = 2      # 2 attempts total (was 3)
_BACKOFF_BASE   = 0.5   # 0.5s first backoff (was 2s)


# ─────────────────────────────────────────────────────────────────────
# Session bundle — carries all tokens needed after the page parse
# Passed from fetch() into extract_new_jobs() to avoid a second page fetch.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _WorkdaySession:
    origin:   str
    api_url:  str
    headers:  dict   # CSRF + auth headers for API calls
    tenant:   str    # Workday tenant identifier (needed for detail URL)
    site:     str    # Workday siteId (needed for detail URL)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _extract_token(html: str) -> Optional[str]:
    for pattern in [
        r'"token"\s*:\s*"([^\"]+)"',
        r'\btoken\s*:\s*"([^\"]+)"',
        r'\btoken\s*=\s*"([^\"]+)"',
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


def _extract_tenant_site(html: str) -> Tuple[Optional[str], Optional[str]]:
    tm = re.search(r'\btenant\s*:\s*"([^\"]+)"', html)
    sm = re.search(r'\bsiteId\s*:\s*"([^\"]+)"', html)
    return (tm.group(1) if tm else None), (sm.group(1) if sm else None)


def _get_origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _normalize_listing_job(raw: dict) -> dict:
    job = dict(raw)
    bullets = job.pop("bulletFields", []) or []
    job["id"] = "|".join(map(str, bullets)) if bullets else str(
        raw.get("jobReqId") or raw.get("id") or raw.get("externalPath") or ""
    )
    return job


def _ensure_path(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("/") else "/" + path


def _page_headers() -> dict:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": REALISTIC_USER_AGENT,
        "Cache-Control": "max-age=0",
    }


def _api_headers(origin_url: str, token: str) -> dict:
    origin = _get_origin(origin_url)
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "x-calypso-csrf-token": token,
        "referer": origin_url,
        "origin": origin,
        "user-agent": REALISTIC_USER_AGENT,
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }


def _fetch_page(http: HttpClient, url: str, timeout: int) -> str:
    """
    Fetch a Workday jobs page and return its HTML.
    Handles redirect payloads and retries only on 403/500/503.
    Backoff is tight (0.5s, 1.0s) to avoid stalling workers.
    """
    headers = _page_headers()

    for attempt in range(_MAX_RETRIES):
        try:
            resp = http.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            html = resp.text

            # Handle Workday redirect payloads
            if html.strip().startswith("{"):
                try:
                    payload = json.loads(html)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("widget") == "redirect":
                    redir = payload.get("url")
                    if redir:
                        url = _get_origin(url) + redir
                        resp = http.get(url, headers=headers, timeout=timeout)
                        resp.raise_for_status()
                        html = resp.text
            return html

        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (403, 500, 503) and attempt < _MAX_RETRIES - 1:
                backoff = _BACKOFF_BASE * (2 ** attempt)
                logger.debug(f"[Workday] HTTP {code}, retry in {backoff:.1f}s")
                time.sleep(backoff)
                continue
            raise
        except requests.exceptions.RequestException:
            raise

    raise ValueError(f"[Workday] Failed to fetch {url} after {_MAX_RETRIES} attempts")


def _parse_session(http: HttpClient, url: str, timeout: int) -> Optional["_WorkdaySession"]:
    """
    Fetch the Workday jobs page and extract CSRF token / tenant / siteId.
    Returns a _WorkdaySession or None if parsing fails.
    """
    try:
        html = _fetch_page(http, url, timeout=timeout)
    except Exception as e:
        logger.debug(f"[Workday] Page fetch failed for {url}: {e}")
        return None

    token = _extract_token(html)
    tenant, site = _extract_tenant_site(html)
    if not token or not tenant or not site:
        logger.debug(f"[Workday] Could not parse token/tenant/site from {url}")
        return None

    origin = _get_origin(url)
    return _WorkdaySession(
        origin=origin,
        api_url=f"{origin}/wday/cxs/{tenant}/{site}/jobs",
        headers=_api_headers(url, token),
        tenant=tenant,
        site=site,
    )


def _fetch_today_jobs(
    http: HttpClient,
    session: "_WorkdaySession",
    schema: dict,
    api_timeout: int,
) -> List[dict]:
    """
    Paginate the Workday jobs API and return all jobs posted today.
    No inter-page sleep — Workday's own response latency is sufficient spacing.
    """
    limit = schema.get("limit", 20) or 20
    offset = 0
    all_today: List[dict] = []

    while True:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        res = http.post(session.api_url, json_body=payload, headers=session.headers, timeout=api_timeout)
        data = res.json()
        jobs = data.get("jobPostings", []) or []

        todays = [j for j in jobs if str(j.get("postedOn", "")).strip() == "Posted Today"]
        if not todays:
            break

        all_today.extend(_normalize_listing_job(j) for j in todays)
        offset += limit
        if len(jobs) < limit:
            break

    return all_today


def _fetch_detail(
    http: HttpClient,
    session: "_WorkdaySession",
    external_path: str,
    api_timeout: int,
) -> dict:
    """
    Fetch a single job's detail JSON from the Workday API.

    Workday listing API returns externalPath as a browser-facing path like:
      /en-US/company_site/job/Location/Job-Title_JR12345
    The JSON detail endpoint is at:
      {origin}/wday/cxs/{tenant}/{site}{externalPath}
    e.g.:
      https://company.wd5.myworkdayjobs.com/wday/cxs/company/site/en-US/...
    """
    endpoint = f"{session.origin}/wday/cxs/{session.tenant}/{session.site}{_ensure_path(external_path)}"
    resp = http.get(endpoint, headers=session.headers, timeout=api_timeout)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def fetch(
    company: Company,
    http: HttpClient,
    schema: dict,
    disable_filter: bool = False,
) -> Tuple[str, List[str]]:
    """
    Fetch today's Workday job IDs for hash comparison.
    Returns (canonical_json, [id, ...]).

    Also stashes the parsed session on the schema dict under "_wd_session"
    so extract_new_jobs() can reuse it without a second page fetch.
    """
    url = company.board_token.strip()
    if not url:
        raise ValueError("Workday company requires a full jobs page URL as board_token")

    page_timeout = schema.get("page_timeout", 10)
    api_timeout  = schema.get("timeout", 15)

    session = _parse_session(http, url, timeout=page_timeout)
    if session is None:
        raise ValueError(f"[Workday] Could not parse session from {url}")

    try:
        today_jobs = _fetch_today_jobs(http, session, schema, api_timeout)
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workday] fetch failed for {company.name}: {e}")
        raise

    ids = sorted({str(j.get("id", "")) for j in today_jobs if j.get("id")})
    canonical = json.dumps(ids)

    # Cache the session for extract_new_jobs() — keyed by board_token so
    # concurrent workers for different companies don't collide.
    schema.setdefault("_wd_sessions", {})[company.board_token] = session

    return canonical, ids


def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    """
    Return Job objects for IDs not in seen_ids.
    Reuses the session cached by fetch() — no redundant page fetch.
    Falls back to a fresh page fetch if the cache is cold (e.g. first run).
    """
    url = company.board_token.strip()
    if not url:
        return []

    page_timeout = schema.get("page_timeout", 10)
    api_timeout  = schema.get("timeout", 15)

    # Reuse session cached by fetch() — avoids a redundant page fetch.
    # Pop it (single-use): CSRF tokens expire, so we never reuse across cycles.
    wd_sessions = schema.get("_wd_sessions", {})
    session: Optional[_WorkdaySession] = wd_sessions.pop(company.board_token, None)
    if session is None:
        # Cold path: fetch() wasn't called first (e.g. first-ever baseline run)
        session = _parse_session(http, url, timeout=page_timeout)
        if session is None:
            logger.warning(f"[Workday] Could not parse session for {company.name}")
            return []

    try:
        today_jobs = _fetch_today_jobs(http, session, schema, api_timeout)
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workday] extract_new_jobs fetch failed for {company.name}: {e}")
        return []

    # Skip detail extraction if configured
    skip_details = schema.get("skip_details", False)

    seen_set = set(seen_ids)
    new_jobs: List[Job] = []

    for normalized in today_jobs:
        job_id = str(normalized.get("id", ""))
        if not job_id or job_id in seen_set:
            continue

        if skip_details:
            # Build job from listing data only — no second HTTP call
            location = normalized.get("locationsText") or ""
            remote   = "remote" in location.lower()
            ext_path = normalized.get("externalPath") or ""
            job_url  = f"{session.origin}{_ensure_path(ext_path)}" if ext_path else ""
            new_jobs.append(Job(
                id=job_id,
                title=normalized.get("title") or "Untitled",
                company=company.name,
                location=location,
                department="",
                url=job_url,
                posted_at=normalized.get("postedOn"),
                remote=remote,
                salary=None,
                description=None,
                raw=normalized,
            ))
            continue

        # Fetch full detail (sequential — no nested thread pool)
        ext_path = normalized.get("externalPath") or ""
        if not ext_path:
            continue
        try:
            details = _fetch_detail(http, session, ext_path, api_timeout)
        except Exception as e:
            logger.warning(f"[Workday] Detail fetch failed for {company.name} job {job_id}: {e}")
            continue

        info    = details.get("jobPostingInfo", {})
        job_url = info.get("externalUrl") or f"{session.origin}{_ensure_path(ext_path)}"

        location = info.get("location") or normalized.get("locationsText") or ""
        country  = info.get("jobRequisitionLocation", {}).get("country", {}).get("descriptor")
        if country:
            location += f" • {country}"
        remote = "remote" in location.lower() if isinstance(location, str) else False

        raw_html = info.get("jobDescription", "")
        description = parse_html_description(raw_html) if raw_html else None

        new_jobs.append(Job(
            id=job_id,
            title=info.get("title") or normalized.get("title") or "Untitled",
            company=company.name,
            location=location,
            department=info.get("timeType") or "",
            url=job_url,
            posted_at=info.get("postedOn") or normalized.get("postedOn"),
            remote=remote,
            salary=None,
            description=description,
            raw=details,
        ))

    return new_jobs
