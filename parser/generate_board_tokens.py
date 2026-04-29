import json
import re
import time
from urllib.parse import urlparse, urlunparse

import requests

# ── Config ────────────────────────────────────────────────────────────────────
INPUT_FILE = "data.json"
TIMEOUT = 10
DELAY = 0.2
MAX_RETRIES = 1

GOOD_STATUS = set(range(200, 400))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ATS-Parser/1.0)"
})

# ── ATS Detection ─────────────────────────────────────────────────────────────
ATS_CONFIG = [
    {"name": "workday", "regex": re.compile(r"\.myworkday(jobs|site)\.com$", re.I)},
    {"name": "greenhouse", "regex": re.compile(r"greenhouse\.io$", re.I)},
    {"name": "lever", "regex": re.compile(r"(^|\.)lever\.co$", re.I)},
    {"name": "ashby", "regex": re.compile(r"ashbyhq\.com$", re.I)},
]

# ── In-memory storage (NO disk writes during processing) ─────────────────────
unique_tokens = {
    "workday": set(),
    "greenhouse": set(),
    "lever": set(),
    "ashby": set(),
    "unknown": set(),
    "errors": set(),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    netloc = urlparse(url).netloc
    for ats in ATS_CONFIG:
        if ats["regex"].search(netloc):
            return ats["name"]
    return "unknown"


def is_working(url: str) -> bool:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = SESSION.head(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 405:
                resp = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
                resp.close()
            return resp.status_code in GOOD_STATUS
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(1)
    return False


def candidate_prefixes(url: str):
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]

    candidates = []
    for n in range(len(segments), -1, -1):
        path = "/" + "/".join(segments[:n]) if n else ""
        candidate = urlunparse(parsed._replace(path=path, query="", fragment=""))
        candidates.append(candidate)

    return list(reversed(candidates))  # shortest → longest


# ── Extractors ───────────────────────────────────────────────────────────────

def extract_workday_token(url: str) -> str | None:
    for candidate in candidate_prefixes(url):
        if is_working(candidate):
            return candidate
        time.sleep(DELAY)
    return None


def extract_greenhouse_token(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0] if parts else None


def extract_lever_token(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0] if parts else None


def extract_ashby_token(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0] if parts else None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    total = len(data)

    for i, item in enumerate(data):
        url = item.get("applyUrl", "").strip()
        print(f"\n[{i+1}/{total}] {url}")

        if not url:
            unique_tokens["errors"].add(url)
            continue

        ats_type = detect_ats(url)
        print(f"  ATS detected: {ats_type}")

        try:
            token = None

            if ats_type == "greenhouse":
                token = extract_greenhouse_token(url)

            elif ats_type == "lever":
                token = extract_lever_token(url)

            elif ats_type == "ashby":
                token = extract_ashby_token(url)

            elif ats_type == "workday":
                token = extract_workday_token(url)

            else:
                unique_tokens["unknown"].add(url)
                continue

            if token:
                unique_tokens[ats_type].add(token)
                print(f"  → Token: {token}")
            else:
                unique_tokens["errors"].add(url)
                print("  → Failed to extract token")

        except Exception as e:
            print(f"  → Error: {e}")
            unique_tokens["errors"].add(url)

        time.sleep(DELAY)

    # ── FINAL WRITE (sorted, deterministic output) ────────────────────────────

    def write_sorted(filename: str, items: set):
        with open(filename, "w", encoding="utf-8") as f:
            for item in sorted(items):
                f.write(item + "\n")

    write_sorted("workday.txt", unique_tokens["workday"])
    write_sorted("greenhouse.txt", unique_tokens["greenhouse"])
    write_sorted("lever.txt", unique_tokens["lever"])
    write_sorted("ashby.txt", unique_tokens["ashby"])
    write_sorted("unknown.txt", unique_tokens["unknown"])
    write_sorted("errors.txt", unique_tokens["errors"])

    print("\n── DONE ──────────────────────────────────────")
    print("All ATS files written in sorted + deduplicated form.")


if __name__ == "__main__":
    main()