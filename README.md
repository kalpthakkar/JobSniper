# 🎯 Job Sniper

**Real-time job posting monitor** — be in the first 5 applicants, not the first 500.

Polls ATS systems directly (Greenhouse, Ashby, Workable, Lever) at sub-minute intervals, detects new postings the moment they appear, and fires instant alerts.

---

## 🏗 Architecture

```
job_sniper/
├── main.py                  ← Entry point (CLI)
├── config.yaml              ← All configuration + company list
├── requirements.txt
│
├── core/
│   ├── models.py            ← Shared data types (Company, Job, Priority, ATSType)
│   ├── config.py            ← Config loader
│   ├── database.py          ← Thread-safe JSON hash store
│   ├── http_client.py       ← UA rotation, proxy support, retry logic
│   └── poller.py            ← CompanyPoller threads + PollOrchestrator
│
├── ats/
│   ├── router.py            ← ATS dispatcher (maps type → adapter)
│   ├── greenhouse.py        ← Greenhouse adapter
│   ├── ashby.py             ← Ashby adapter
│   ├── workable.py          ← Workable adapter
│   └── lever.py             ← Lever adapter
│
├── notifications/
│   └── notifier.py          ← Console / Telegram / Webhook alerts
│
└── data/
    └── job_db.json          ← Auto-created; stores hashes + seen job IDs
```

---

## ⚡ Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. List tracked companies

```bash
python main.py --list
```

### 3. Debug a single company (one-shot probe)

```bash
python main.py --company stripe
python main.py --company openai
```

### 4. Start full monitoring

```bash
python main.py
```

On **first run**, the system seeds the database with all currently open jobs — no alerts. From the **second run onward**, any new job ID triggers an instant notification.

---

## ⚙️ Configuration (`config.yaml`)

### Poll intervals (seconds)

```yaml
system:
  poll_intervals:
    HIGH: 30      # 30 seconds — hot targets
    MID:  300     # 5 minutes
    LOW:  1800    # 30 minutes
  max_workers: 20 # max parallel threads
```

### Add a new company

```yaml
companies:
  - name: Linear
    board_token: linear
    ats: ashby           # greenhouse | ashby | workable | workday | lever
    priority: HIGH
```

### Enable Telegram alerts

```yaml
system:
  notify_channels:
    - console
    - telegram
  telegram:
    bot_token: "123456:ABC..."
    chat_id: "@yourchannel"
```

### Enable Webhook

```yaml
system:
  notify_channels:
    - console
    - webhook
  webhook:
    url: "https://hooks.zapier.com/..."
    headers:
      Authorization: "Bearer token"
```

---

## 🛡 Anti-Block Strategy

The HTTP client implements:

| Strategy | Description |
|---|---|
| **UA rotation** | 15+ real Chrome/Firefox/Safari agents, rotated randomly |
| **Jitter** | Random 0.1–0.5s delay per request (humanisation) |
| **Exponential backoff** | Auto-retries on 429/5xx with increasing delays |
| **Proxy rotation** | Optional — add proxies to `proxies.txt` (one per line) |
| **Connection pooling** | Efficient session reuse, not a new connection per poll |

To enable proxies:

```yaml
system:
  ip_strategy: rotating_proxies
  proxy_file: proxies.txt
```

`proxies.txt` format:
```
http://user:pass@proxy1.host:8080
http://user:pass@proxy2.host:8080
```

---

## 🧠 How Detection Works

```
Poll cycle:
  1. Fetch raw JSON from ATS endpoint
  2. SHA-256 hash the response
  3. Compare to stored hash
        No change → sleep → repeat
        Changed   → extract job IDs not in seen_ids
                  → notify
                  → update DB with new hash + merged IDs
```

- **First run** → baseline is set, no alert (prevents false positives on startup)
- **Hash change, no new IDs** → a job was edited or removed (logged, not alerted)
- **New IDs** → alert fired with full job metadata

---

## 📡 ATS Endpoints Used

| ATS | Endpoint |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/{token}` |
| Workday | Full Workday recruiting URL (e.g. `https://walmart.wd5.myworkdayjobs.com/en-US/walmartexternal`) |
| Lever | `https://api.lever.co/v0/postings/{token}?mode=json&limit=50` |

All are **public, no auth required**.

---

## 🔧 Adding a New ATS

1. Create `ats/myats.py` with two functions:
   ```python
   def fetch(company, http, schema) -> Tuple[str, List[str]]: ...
   def extract_new_jobs(company, http, schema, seen_ids) -> List[Job]: ...
   ```
2. Add `myats` to `ATSType` enum in `core/models.py`
3. Register it in `ats/router.py`
4. Add endpoint schema to `config.yaml` under `ats_schemas`

---

## 📋 CLI Reference

```
python main.py                        Full monitoring loop
python main.py --config path.yaml     Custom config
python main.py --company stripe       One-shot probe (debug)
python main.py --list                 List all companies
```

---

## 🌐 Distributed Setup (Multiple Machines)

For high-volume monitoring, split companies across multiple machines using NATS messaging.

### Architecture

```
Worker Machines (job_sniper) → NATS Server → Aggregator Machine (JobAggregator)
```

### 1. Start NATS Server

```bash
# Install NATS server
go install github.com/nats-io/nats-server/v2@latest

# Start server (accessible on LAN)
nats-server -a 192.168.1.100  # Replace with your LAN IP
```

### 2. Configure Workers

Update `config.yaml` on each worker machine:

```yaml
system:
  notify_channels:
    - nats  # Enable NATS publishing instead of direct notifications
  nats:
    servers: ["nats://192.168.1.100:4222"]  # Point to NATS server
    subject: "job_sniper.jobs"
```

Use `--token-start` and `--token-stop` to split companies:

```bash
# Worker 1: First 50 companies
python main.py --token-start 0 --token-stop 49

# Worker 2: Next 50 companies  
python main.py --token-start 50 --token-stop 99

# Worker 3: Remaining companies
python main.py --token-start 100
```

### 3. Start Aggregator

On the aggregator machine:

```bash
cd JobAggregator
pip install -r requirements.txt
python aggregator.py
```

### 4. API Access

The aggregator provides REST endpoints:

```bash
# Get highest-priority job
curl http://localhost:5001/best-job

# Get queue statistics  
curl http://localhost:5001/queue-stats
```

### Job Scoring

Jobs are prioritized by:
- **ATS Score**: Greenhouse (10), Lever (9), Ashby (8), Workday (7), Workable (6)
- **Recency**: Exponential decay (half-life ~1 day)
- **Priority Queue**: Top jobs maintained for instant access

---

## ⚠️ Legal & Ethical Notes

- Only hits **public, unauthenticated** ATS endpoints — these are designed to be read
- Rate limits respected via backoff; **not a scraper**
- Adding `robots.txt` checks and `Retry-After` header support is recommended for production use
- Always review a company's Terms of Service before monitoring at high frequency
