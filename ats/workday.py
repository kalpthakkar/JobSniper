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
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.http_client import HttpClient
from core.models import Company, Job

logger = logging.getLogger("job_sniper.ats.workday")


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
        "user-agent": "Mozilla/5.0",
    }


def _ensure_path(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("/") else "/" + path


def _fetch_workday_page(http: HttpClient, url: str) -> str:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = http.get(url, headers=headers)
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
                resp = http.get(url, headers=headers)
                html = resp.text

    return html


def _fetch_job_details(
    http: HttpClient,
    origin: str,
    tenant: str,
    site: str,
    external_path: str,
    headers: dict,
) -> dict:
    endpoint = f"{origin}/wday/cxs/{tenant}/{site}{_ensure_path(external_path)}"
    resp = http.get(endpoint, headers=headers)
    return resp.json()


def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    url = company.board_token.strip()
    if not url:
        raise ValueError("Workday company must provide a full Workday jobs page URL")

    try:
        html = _fetch_workday_page(http, url)
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

            res = http.post(api, json_body=payload, headers=headers)
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

    try:
        html = _fetch_workday_page(http, url)
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

            res = http.post(api, json_body=payload, headers=headers)
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
                details = _fetch_job_details(http, origin, tenant, site, external_path, headers)
                info = details.get("jobPostingInfo", {})
                org = details.get("hiringOrganization", {})
                job_url = info.get("externalUrl") or f"{origin}{_ensure_path(external_path)}"

                location = info.get("location") or normalized.get("locationsText") or ""
                remote = "remote" in location.lower() if isinstance(location, str) else False

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
                    raw=details,
                ))

            offset += limit
            if len(jobs) < limit:
                break

        return new_jobs
    except requests.exceptions.RequestException as e:
        logger.error(f"[Workday] extract_new_jobs failed for {company.name}: {e}")
        return []
