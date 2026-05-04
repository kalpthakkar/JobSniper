"""
ats/workday.py — Workday ATS adapter.

Workday does not use a single board token. Instead the company entry is the
public Workday jobs page URL itself. The adapter fetches the page, extracts the
CSRF token / tenant / siteId values, and paginates the jobs API while filtering
for `Posted Today` jobs only.
"""
import json
import logging
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.http_client import HttpClient
from core.models import Company, Job
from core.description_parser import parse_html_description

logger = logging.getLogger("job_sniper.ats.workday")

# Realistic User-Agent to avoid bot detection
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Rate limiting: delay between requests (seconds)
REQUEST_DELAY = 0.5
PAGINATION_DELAY = 1.0

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 2  # seconds


def _extract_token(html: str) -> Optional[str]:
    patterns = [
        r'"token"\s*:\s*"([^\"]+)"',
        r'\btoken\s*:\s*"([^\"]+)"',
        r'\btoken\s*=\s*"([^\"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _extract_tenant_site(html: str) -> Tuple[Optional[str], Optional[str]]:
    tenant_match = re.search(r'\btenant\s*:\s*"([^\"]+)"', html)
    site_match = re.search(r'\bsiteId\s*:\s*"([^\"]+)"', html)
    tenant = tenant_match.group(1) if tenant_match else None
    site = site_match.group(1) if site_match else None
    return tenant, site


def _get_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_listing_job(raw: dict) -> dict:
    job = dict(raw)
    bullets = job.pop("bulletFields", []) or []
    if bullets:
        job_id = "|".join(map(str, bullets))
    else:
        job_id = str(raw.get("jobReqId") or raw.get("id") or raw.get("externalPath") or "")
    job["id"] = job_id
    return job


def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    lines: List[str] = []

    for tag in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name in ["h1", "h2", "h3"]:
            lines.append(f"\n{text.upper()}\n")
        elif tag.name == "li":
            lines.append(f"• {text}")
        else:
            lines.append(text)

    return "\n".join(lines)


def _build_headers(origin_url: str, token: str) -> dict:
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


def _build_page_headers() -> dict:
    """Headers for initial page fetch to mimic real browser."""
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": REALISTIC_USER_AGENT,
        "Cache-Control": "max-age=0",
    }


def _ensure_path(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("/") else "/" + path


def _fetch_workday_page(http: HttpClient, url: str, timeout: int) -> str:
    """Fetch Workday page with retry logic and proper headers."""
    headers = _build_page_headers()
    
    for attempt in range(MAX_RETRIES):
        try:
            resp = http.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            html = resp.text

            if html.strip().startswith("{"):
                try:
                    payload = json.loads(html)
                except json.JSONDecodeError:
                    payload = None

                if isinstance(payload, dict) and payload.get("widget") == "redirect":
                    redirect_url = payload.get("url")
                    if redirect_url:
                        url = _get_origin(url) + redirect_url
                        # Add delay before redirect request
                        time.sleep(REQUEST_DELAY)
                        resp = http.get(url, headers=headers, timeout=timeout)
                        resp.raise_for_status()
                        html = resp.text

            return html
            
        except requests.exceptions.HTTPError as e:
            # 403, 500 errors should retry with backoff
            if e.response.status_code in [403, 500, 503]:
                if attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logger.debug(f"[Workday] Rate limited ({e.response.status_code}), retrying in {backoff}s (attempt {attempt+1}/{MAX_RETRIES})")
                    time.sleep(backoff)
                    continue
            raise
        except requests.exceptions.RequestException:
            raise
    
    raise ValueError(f"Failed to fetch {url} after {MAX_RETRIES} attempts")



def _fetch_job_details(
    http: HttpClient,
    origin: str,
    tenant: str,
    site: str,
    external_path: str,
    headers: dict,
    timeout: int,
) -> dict:
    endpoint = f"{origin}/wday/cxs/{tenant}/{site}{_ensure_path(external_path)}"
    resp = http.get(endpoint, headers=headers, timeout=timeout)
    return resp.json()


def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    url = company.board_token.strip()
    if not url:
        raise ValueError("Workday company must provide a full Workday jobs page URL")

    timeout = schema.get("timeout", 10)
    try:
        html = _fetch_workday_page(http, url, timeout=timeout)
        token = _extract_token(html)
        tenant, site = _extract_tenant_site(html)

        if not token or not tenant or not site:
            raise ValueError(
                f"Could not extract Workday token/tenant/site from {url}. "
                "Verify the provided URL is a public Workday jobs page."
            )

        origin = _get_origin(url)
        api = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
        headers = _build_headers(url, token)

        all_jobs: List[dict] = []
        offset = 0
        limit = schema.get("limit", 20) or 20
        total: Optional[int] = None

        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }

            # Add delay before pagination request to avoid rate limiting
            if offset > 0:
                time.sleep(PAGINATION_DELAY)
            
            res = http.post(api, json_body=payload, headers=headers, timeout=timeout)
            data = res.json()
            jobs = data.get("jobPostings", []) or []

            if total is None:
                total = data.get("total", 0)

            todays = [job for job in jobs if str(job.get("postedOn", "")).strip() == "Posted Today"]
            if not todays:
                break

            all_jobs.extend(_normalize_listing_job(job) for job in todays)

            offset += limit
            if len(jobs) < limit:
                break

        ids = sorted({str(job.get("id", "")) for job in all_jobs if job.get("id")})
        canonical = json.dumps(ids)
        return canonical, ids
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workday] Failed to fetch {company.name}: {e}")
        raise


def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    url = company.board_token.strip()
    if not url:
        return []

    timeout = schema.get("timeout", 10)
    try:
        html = _fetch_workday_page(http, url, timeout=timeout)
        token = _extract_token(html)
        tenant, site = _extract_tenant_site(html)

        if not token or not tenant or not site:
            logger.warning(f"[Workday] Missing token/tenant/site for {company.name}")
            return []

        origin = _get_origin(url)
        api = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
        headers = _build_headers(url, token)

        new_jobs: List[Job] = []
        offset = 0
        limit = schema.get("limit", 20) or 20

        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }

            # Add delay before pagination request to avoid rate limiting
            if offset > 0:
                time.sleep(PAGINATION_DELAY)

            res = http.post(api, json_body=payload, headers=headers, timeout=timeout)
            data = res.json()
            jobs = data.get("jobPostings", []) or []
            todays = [job for job in jobs if str(job.get("postedOn", "")).strip() == "Posted Today"]
            if not todays:
                break

            for raw in todays:
                normalized = _normalize_listing_job(raw)
                job_id = str(normalized.get("id", ""))
                if not job_id or job_id in seen_ids:
                    continue

                external_path = normalized.get("externalPath") or raw.get("externalPath") or ""
                
                # Add delay before fetching job details
                time.sleep(REQUEST_DELAY)
                details = _fetch_job_details(http, origin, tenant, site, external_path, headers, timeout=timeout)
                info = details.get("jobPostingInfo", {})
                org = details.get("hiringOrganization", {})
                job_url = info.get("externalUrl") or f"{origin}{_ensure_path(external_path)}"

                location = info.get("location") or normalized.get("locationsText") or ""
                country_descriptor = info.get("jobRequisitionLocation", {}).get("country", {}).get('descriptor')
                if country_descriptor:
                    location += f' • {country_descriptor}'
                remote = "remote" in location.lower() if isinstance(location, str) else False

                # Extract and parse job description from jobDescription field (HTML format)
                raw_description_html = info.get("jobDescription", "")
                description = parse_html_description(raw_description_html) if raw_description_html else None

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

            offset += limit
            if len(jobs) < limit:
                break

        return new_jobs
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workday] extract_new_jobs failed for {company.name}: {e}")
        return []
