"""
core/http_client.py — Hardened HTTP client.

Features:
  - Random User-Agent rotation (100+ agents)
  - Optional proxy rotation from file
  - Exponential backoff with jitter on retries
  - Connection pooling via requests.Session
"""
import random
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("job_sniper.http")

# ------------------------------------------------------------------
# Large pool of real-world user agents
# ------------------------------------------------------------------
USER_AGENTS: List[str] = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Mobile Chrome
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/123.0.6312.52 Mobile/15E148 Safari/604.1",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,fr;q=0.7",
    "en-US,en;q=0.9,de;q=0.8",
]


def _build_session(timeout: int, max_retries: int, retry_delay: float) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=retry_delay,
        # ⚠️  429 is intentionally NOT in this list.
        # urllib3's auto-retry on 429 sleeps: backoff_factor * 2^n seconds per attempt.
        # With backoff_factor=2 and 3 retries that's 2+4+8 = 14s of thread-blocking
        # per request. With 20 workers all hitting Ashby 429s simultaneously, every
        # executor slot is frozen for 14s → dispatcher starves → system pauses for minutes.
        # Instead, 429 surfaces immediately as HTTPError; our caller (ATS adapters)
        # raises RateLimitError, which the scheduler handles with a global cooldown
        # without occupying any worker thread.
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    # OPTIMIZATION (Phase 1): Increased pool from 50->100 connections, 100->200 max size
    # This allows more concurrent requests without queue contention
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=200)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _random_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    }


class HttpClient:
    """
    Thread-safe HTTP client with UA rotation + optional proxy rotation.
    One instance is shared across all poller threads.
    """

    def __init__(
        self,
        timeout: int = 10,
        max_retries: int = 3,
        retry_delay: float = 0.5,   # backoff_factor for 5xx retries (was 2 → caused 14s blocks)
        proxy_file: str = "",
        ip_strategy: str = "user_agent_rotation",
    ):
        self.timeout = timeout
        self.ip_strategy = ip_strategy
        self._proxies: List[str] = self._load_proxies(proxy_file)
        self._proxy_idx = 0
        self._session = _build_session(timeout, max_retries, retry_delay)

    # ------------------------------------------------------------------
    def _load_proxies(self, proxy_file: str) -> List[str]:
        if not proxy_file:
            return []
        path = Path(proxy_file)
        if not path.exists():
            logger.warning(f"Proxy file not found: {proxy_file} — running without proxies")
            return []
        lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
        logger.info(f"Loaded {len(lines)} proxies from {proxy_file}")
        return lines

    def _next_proxy(self) -> Optional[Dict[str, str]]:
        if not self._proxies:
            return None
        proxy = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return {"http": proxy, "https": proxy}

    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        params: Optional[Dict] = None,
        timeout: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        base_headers = _random_headers()
        if headers:
            base_headers.update(headers)

        # Tiny jitter to humanise requests.
        # Kept at the low end: each worker thread sleeping even 50 ms means
        # 20 workers waste 1 s of collective executor time per dispatch cycle.
        # The scheduler's adaptive_gap already handles pacing.
        jitter = random.uniform(0.01, 0.05)
        time.sleep(jitter)

        kwargs: Dict[str, Any] = {
            "headers": base_headers,
            "timeout": timeout if timeout is not None else self.timeout,
            "params": params or {},
        }

        if self.ip_strategy == "rotating_proxies":
            proxy = self._next_proxy()
            if proxy:
                kwargs["proxies"] = proxy

        try:
            response = self._session.get(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                # Rate-limited — signal to caller (scheduler will apply global throttle)
                logger.warning(f"Rate limited (429) on {url} — global throttle will be applied")
            raise

    def post(
        self,
        url: str,
        json_body: dict,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        base_headers = _random_headers()
        base_headers["Content-Type"] = "application/json"
        if headers:
            base_headers.update(headers)

        jitter = random.uniform(0.01, 0.05)
        time.sleep(jitter)

        kwargs: Dict[str, Any] = {
            "headers": base_headers,
            "timeout": timeout if timeout is not None else self.timeout,
            "json": json_body,
        }
        if self.ip_strategy == "rotating_proxies":
            proxy = self._next_proxy()
            if proxy:
                kwargs["proxies"] = proxy

        try:
            response = self._session.post(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                # Rate-limited — signal to caller (scheduler will apply global throttle)
                logger.warning(f"Rate limited (429) on {url} — global throttle will be applied")
            raise