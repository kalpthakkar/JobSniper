# Apple Careers Adapter Integration

## Overview

Successfully integrated a complete Apple Careers adapter into job_sniper. This is a company-specific adapter (similar to Google and Tesla) that runs independently and fetches jobs from Apple's careers API.

## Features

### ✅ Complete Implementation

1. **API Integration** (`company/apple/apple.py`)
   - CSRF token management for API authentication
   - Pagination support (20 jobs per page)
   - 6-hour job filtering (only recent jobs)
   - Graceful error handling and timeout management

2. **Job Details Fetcher** (`company/apple/apple.py`)
   - Hydration data extraction from job detail pages
   - HTML parsing and description assembly
   - Supports sections: Summary, Description, Responsibilities, Qualifications, Requirements

3. **Dedicated Poller** (`core/apple_poller.py`)
   - Independent polling thread with cooldown intervals
   - 6-hour recent job window filtering
   - Change detection (new jobs, removed jobs)
   - Disappearance policy (3-strike rule for job removal)
   - Notification integration for new jobs

4. **Frontend Integration**
   - Settings page toggle for enabling/disabling Apple Careers poller
   - Dashboard stats showing Apple adapter status
   - Real-time enable/disable capability

5. **Configuration**
   - `config.yaml`: Apple polling settings (cooldown, timeout)
   - `core/config.py`: Configuration loading
   - Database settings: Persistent enable/disable state

## Response Schema

Apple's API returns paginated job listings:

```json
{
  "res": {
    "searchResults": [
      {
        "id": "PIPE-200314015",
        "jobSummary": "Job description summary",
        "postingTitle": "Job Title",
        "positionId": "200314015",
        "postDateInGMT": "2026-04-26T22:24:21Z",
        "locations": [
          {
            "name": "Location name",
            "countryName": "Country"
          }
        ],
        "team": {
          "teamName": "Team name"
        },
        "homeOffice": false
      }
    ],
    "totalRecords": 6551
  }
}
```

## Key Features

### 6-Hour Filtering
- Automatically filters jobs to only recent postings (last 6 hours)
- Uses ISO 8601 `postDateInGMT` field for accurate filtering
- Efficient pagination: stops fetching when all jobs are outside the time window

### Remote Job Detection
- Checks `homeOffice` boolean flag
- Also searches location string for "remote" keyword
- Ensures accurate remote job categorization

### Location Extraction
- Combines location name + country name (format: "Name • Country")
- Handles multiple locations per job
- Graceful handling of missing country names

### Job Description Assembly
- Fetches individual job detail pages
- Extracts hydration data from page HTML
- Assembles multi-part descriptions:
  - Summary (jobSummary)
  - Description (description)
  - Responsibilities (jobResponsibilities)
  - Preferred Qualifications (preferredQualifications)
  - Minimum Requirements (minimumQualifications)
- Converts HTML lists to bullet points

## Files Created/Modified

### New Files
- `company/apple/__init__.py` - Package marker
- `company/apple/apple.py` - Main adapter and detail fetcher (380+ lines)
- `core/apple_poller.py` - Dedicated poller (250+ lines)
- `test_apple_adapter.py` - Comprehensive test suite (280+ lines)

### Modified Files
- `main.py` - Import and initialize ApplePoller
- `config.yaml` - Add Apple settings (apple_enabled, apple_cooldown_minutes, apple_request_timeout)
- `core/config.py` - Load Apple config settings
- `templates/settings.html` - Add Apple toggle to company adapters
- `web/dashboard.py` - Add Apple to settings routes and stats

## Configuration

In `config.yaml`:

```yaml
  # Apple Careers polling
  apple_enabled: true
  apple_cooldown_minutes: 3
  apple_request_timeout: 10
```

## Usage

The Apple adapter operates independently and automatically:

1. **Enable/Disable**: Toggle via dashboard Settings page → "Apple Careers"
2. **Monitoring**: Starts automatically when app launches and enabled
3. **Notifications**: Sends notifications for new jobs via all configured channels (console, Telegram, webhook)
4. **Database**: Tracks job IDs to detect new/removed positions

## API Flow

1. Fetch CSRF token from `/api/v1/CSRFToken`
2. POST to `/api/v1/search` with pagination parameters
3. Filter results by posting date (last 6 hours only)
4. For new jobs, fetch individual job detail pages
5. Extract and parse job descriptions from hydration data
6. Create Job objects and notify

## Polling Cycle

1. Fetch total page count from first request
2. Paginate through all results
3. Filter to jobs posted in last 6 hours
4. Compare IDs with database (detect new jobs)
5. Apply disappearance policy (3-strike rule for removals)
6. Fetch details for new jobs in parallel (max 5 concurrent)
7. Notify about new jobs
8. Update database with new job set
9. Cooldown and repeat

## Performance

- **Timeout**: 10 seconds per API request, 10 seconds for detail page fetch
- **Concurrent Fetches**: 5 parallel detail fetches for new jobs
- **Polling Interval**: 3 minutes (configurable)
- **Time Window**: 6 hours (fixed, reduces database bloat)

## Testing

Run tests:

```bash
pytest test_apple_adapter.py -v
```

Test coverage includes:
- Location extraction (single, multiple, no country)
- Remote detection (homeOffice flag, location string)
- API response parsing
- Job description assembly
- HTML to bullet conversion
- Job object construction
- Sample real API response parsing

## Error Handling

- CSRF token failures: Graceful retry with logging
- Network timeouts: Logged and skipped (doesn't crash poller)
- JSON parsing errors: Logged with context
- Missing fields: Graceful fallbacks (empty strings, None values)
- Hydration data not found: Skips job detail, logs warning

## Integration with Existing System

- Uses same Job model as other adapters
- Integrates with Notifier for all 3 channels (console, Telegram, webhook)
- Uses description_parser for HTML to plain text conversion
- Database tracking same as Tesla/Google adapters
- Settings management through dashboard

## Next Steps (Optional)

1. Workable adapter (final ATS) - similar pattern to Greenhouse/Lever
2. Additional company adapters (if needed)
3. Performance tuning if Apple increases job volume

## Notes

- Apple's API is more straightforward than Tesla (no Playwright needed)
- Job detail pages require separate fetch (not included in list API)
- 6-hour filtering ensures only "fresh" jobs are tracked
- CSRF token must be renewed if it expires (automatic retry)
