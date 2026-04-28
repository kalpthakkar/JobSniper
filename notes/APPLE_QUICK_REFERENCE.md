# Apple Adapter - Quick Reference

## Enabling/Disabling

**Via Dashboard:**
- Navigate to Settings → Company-Specific Adapters
- Toggle "Apple Careers" on/off
- Changes apply immediately

**Via Database:**
```python
from core.database import JobDatabase
db = JobDatabase("data/job_db.db")
db.set_setting("company_apple", "true")   # Enable
db.set_setting("company_apple", "false")  # Disable
```

## Configuration

**config.yaml:**
```yaml
apple_enabled: true              # Enable/disable
apple_cooldown_minutes: 3        # Poll every 3 minutes
apple_request_timeout: 10        # 10 second timeout
```

## Key Classes

### AppleAdapter
- **Location**: `company/apple/apple.py`
- **Purpose**: Fetch and parse job listings from API
- **Key Methods**:
  - `fetch_page(page)` - Get jobs for page
  - `fetch_total_pages()` - Get total page count
  - `fetch_all_recent_jobs(hours=6)` - Get jobs from last N hours
  - `extract_location(job)` - Format location string
  - `is_remote(job)` - Check if remote

### AppleJobDetailFetcher
- **Location**: `company/apple/apple.py`
- **Purpose**: Fetch and parse job detail pages
- **Key Methods**:
  - `fetch_job_detail(position_id)` - Get job detail page
  - `build_job_description(job_data)` - Assemble full description
  - `_html_to_bullets(html)` - Convert HTML lists to bullets

### ApplePoller
- **Location**: `core/apple_poller.py`
- **Purpose**: Independent polling thread for Apple jobs
- **Key Methods**:
  - `start()` - Start polling
  - `stop()` - Stop polling
  - `_poll_cycle()` - Single poll iteration
  - `_fetch_new_job_details()` - Get details for new jobs

## Response Structure

### Job Listing API
- **Endpoint**: `https://jobs.apple.com/api/v1/search`
- **Method**: POST
- **Key Response Fields**:
  - `res.searchResults[]` - Array of job objects
  - `res.totalRecords` - Total job count
  - `searchResults[].id` - Unique job ID
  - `searchResults[].positionId` - Position ID for detail page
  - `searchResults[].postDateInGMT` - Job posting date (ISO 8601)
  - `searchResults[].postingTitle` - Job title
  - `searchResults[].locations[]` - Location information
  - `searchResults[].homeOffice` - Remote job flag
  - `searchResults[].team` - Team/department info

### Job Detail Page
- **URL Pattern**: `https://jobs.apple.com/en-us/details/{positionId}`
- **Data**: Embedded in `window.__staticRouterHydrationData` JSON
- **Key Fields**:
  - `jobSummary` - Summary text
  - `description` - Description text
  - `responsibilities` - HTML list of responsibilities
  - `preferredQualifications` - HTML list of qualifications
  - `minimumQualifications` - HTML list of requirements

## Time Window

- **Fixed to**: Last 6 hours
- **Based on**: `postDateInGMT` field (ISO 8601 format)
- **Purpose**: Reduce database bloat, track only fresh jobs
- **Note**: Not user-configurable (by design)

## Notification Integration

New Apple jobs trigger notifications via:
- **Console**: "Apple: [Job Title] [Location]"
- **Telegram**: Message with job details (100 char preview)
- **Webhook**: JSON POST with full description

All channels respect channel-specific settings in dashboard.

## Database Tracking

**Endpoint**: `apple` (company)
**Type**: `company` (not `ats`)
**Fields Stored**:
- `seen_ids` - Current job IDs
- `metadata.disappearance_counts` - 3-strike removal tracking
- Hash for change detection

## Error Handling

| Error | Behavior |
|-------|----------|
| CSRF token fails | Automatic retry with new token |
| Network timeout | Skip request, log warning, continue polling |
| JSON parsing error | Log error, skip job, continue |
| Missing fields | Use defaults (empty strings/None) |
| Hydration data not found | Skip detail fetch, use basic info only |

## Performance Tips

1. **Reduce Concurrent Fetches**: Modify `max_workers=5` in `ApplePoller._fetch_new_job_details()`
2. **Increase Timeout**: Change `apple_request_timeout` in config
3. **Change Polling Interval**: Modify `apple_cooldown_minutes` in config
4. **Check Logs**: Set `log_level: DEBUG` for verbose output

## Debugging

**Enable Debug Logging:**
```yaml
log_level: DEBUG
```

**Check Settings:**
```bash
curl http://localhost:5000/api/debug/settings
```

**Monitor Polling:**
```bash
tail -f logs.txt | grep "\[apple\]"
```

## Common Issues

**Issue**: Apple Careers not polling
- **Solution**: Check dashboard Settings → Apple Careers toggle
- **Check**: `db.get_setting("company_apple")` should be "true"

**Issue**: No new jobs being detected
- **Solution**: Jobs must be posted in last 6 hours
- **Check**: `postDateInGMT` field in API response

**Issue**: CSRF token errors
- **Solution**: Automatic retry, usually resolves
- **Monitor**: Logs for repeated "[apple] CSRF token obtained" messages

**Issue**: Job descriptions missing
- **Solution**: May not exist on job detail page
- **Check**: Try visiting Apple job page directly

## API Limits

- **No documented rate limit**: Appears to allow reasonable request volume
- **CSRF Token**: Appears to be session-based, refreshes automatically
- **Timeout**: Set to 10 seconds (safe default)

## File Locations

| File | Purpose |
|------|---------|
| `company/apple/__init__.py` | Package marker |
| `company/apple/apple.py` | Adapter implementation |
| `core/apple_poller.py` | Polling logic |
| `test_apple_adapter.py` | Test suite |
| `APPLE_ADAPTER_IMPLEMENTATION.md` | Detailed docs |
| `APPLE_INTEGRATION_COMPLETE.md` | Integration summary |

## Testing

```bash
# Run tests
python -m pytest test_apple_adapter.py -v

# Quick import check
python -c "from company.apple.apple import AppleAdapter; print('OK')"

# Verify config loads
python -c "from core.config import Config; c = Config(); print('apple_enabled:', c.apple_enabled)"
```

## Sample Job Object

```python
Job(
    id="PIPE-200314015",
    title="Genius",
    company="Apple",
    location="India • India",
    department="Apple Retail",
    url="https://jobs.apple.com/en-us/details/200314015",
    remote=False,
    description="Summary:\nApple Retail is where...\n\nResponsibilities:\n• Task 1\n• Task 2\n...",
    posted_at=None,
    salary=None,
    raw={...}  # Original API response
)
```

## Integration Checklist

- ✅ Import ApplePoller in main.py
- ✅ Create apple_poller instance
- ✅ Add to start/stop logic
- ✅ Config file has Apple settings
- ✅ core/config.py loads Apple settings
- ✅ Dashboard UI shows Apple toggle
- ✅ database.py initialized (no changes needed)
- ✅ Notifications configured
- ✅ Tests passing

All items completed and verified!
