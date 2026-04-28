"""
core/filter.py — Unified job filtering system.

This module handles:
1. Notification filters (from database config)
2. Preference filters (user job preferences)

Filters are applied in sequence:
  jobs → notification_rules → preference_filters → final results
"""
import logging
import re
from typing import Any, List, Optional

from core.models import Job

from core.filters.access_restriction_filter import has_access_restrictions

logger = logging.getLogger("job_sniper.filter")


# ─────────────────────────────────────────────────────────────────────────
# CLEARANCE & CITIZENSHIP DETECTION
# ─────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────
# NOTIFICATION FILTERS
# ─────────────────────────────────────────────────────────────────────────

def apply_notification_filters(jobs: List[Job], db: Optional[Any] = None) -> List[Job]:
    """
    Apply notification rule filters to jobs.
    
    This filters based on job_title, company_name, and location rules
    (both inclusive and exclusive).
    
    Args:
        jobs: List of jobs to filter
        db: JobDatabase instance (for reading notification config)
    
    Returns:
        Filtered list of jobs that pass notification rules
    """
    config = {}
    if db is not None:
        config = db.get_notification_config() or {}

    if not config.get("enabled", False):
        return jobs

    def normalize(text: str, case_sensitive: bool) -> str:
        return text if case_sensitive else text.lower()

    def rule_matches(text: str, rule: dict) -> bool:
        value = str(rule.get("value", "")).strip()
        if not value:
            return False
        case_sensitive = bool(rule.get("case_sensitive", False))
        text = normalize(text or "", case_sensitive)
        pattern = normalize(value, case_sensitive)
        match_type = rule.get("match", "includes")
        if match_type == "starts_with":
            return text.startswith(pattern)
        if match_type == "ends_with":
            return text.endswith(pattern)
        return pattern in text

    def section_passes_inclusive(text: str, section: dict) -> bool:
        """Inclusive filter: exclude if none of the rules match (OR logic)."""
        if not section.get("enabled", False):
            return True  # Disabled = allow all
        rules = section.get("rules", []) or []
        if not rules:
            return True  # No rules = allow all
        return any(rule_matches(text, rule) for rule in rules)

    def section_passes_exclusive(text: str, section: dict) -> bool:
        """Exclusive filter (blacklist): exclude if any of the rules match."""
        if not section.get("enabled", False):
            return True  # Disabled = allow all (don't exclude)
        rules = section.get("rules", []) or []
        if not rules:
            return True  # No rules = allow all
        # Return False if ANY rule matches (i.e., exclude the job)
        return not any(rule_matches(text, rule) for rule in rules)

    filtered = []
    for job in jobs:
        # Apply inclusive filters (job_title, company_name, location)
        if not section_passes_inclusive(job.title, config.get("job_title", {})):
            continue
        if not section_passes_inclusive(job.company, config.get("company_name", {})):
            continue
        if not section_passes_inclusive(job.location, config.get("location", {})):
            continue

        # Apply exclusive filters (blacklist_job_title, blacklist_company_name, blacklist_location)
        if not section_passes_exclusive(job.title, config.get("blacklist_job_title", {})):
            continue
        if not section_passes_exclusive(job.company, config.get("blacklist_company_name", {})):
            continue
        if not section_passes_exclusive(job.location, config.get("blacklist_location", {})):
            continue

        filtered.append(job)

    logger.info(f"[filter] {len(filtered)}/{len(jobs)} jobs passed notification rules")
    return filtered


# ─────────────────────────────────────────────────────────────────────────
# PREFERENCE FILTERS
# ─────────────────────────────────────────────────────────────────────────

def apply_preference_filters(jobs: List[Job], db: Optional[Any] = None) -> List[Job]:
    """
    Apply user preference filters to jobs.
    
    This filters based on job preferences (clearance, citizenship, etc).
    
    Args:
        jobs: List of jobs to filter
        db: JobDatabase instance (for reading preferences)
    
    Returns:
        Filtered list of jobs that match user preferences
    """
    if db is None:
        return jobs  # No database = no filtering

    preferences = db.get_preferences() or {}

    # Clearance & Citizenship Preferences
    clearance_pref = preferences.get("access_restriction", "no_preference")

    filtered = []
    for job in jobs:
        has_access_restriction = has_access_restrictions(job)

        # Apply clearance preference logic
        if clearance_pref == "no_preference":
            # No preference: include all jobs
            filtered.append(job)
        elif clearance_pref == "include":
            # Include these jobs: show jobs with AND without clearance/citizenship/export-control
            filtered.append(job)
        elif clearance_pref == "only_show":
            # Only show: include ONLY jobs WITH clearance/citizenship/export-control requirement
            if has_access_restriction:
                filtered.append(job)
        elif clearance_pref == "exclude":
            # Exclude these jobs: include ONLY jobs WITHOUT clearance/citizenship/export-control
            if not has_access_restriction:
                filtered.append(job)

    logger.info(f"[filter] {len(filtered)}/{len(jobs)} jobs passed preference filters")
    return filtered


# ─────────────────────────────────────────────────────────────────────────
# COMBINED FILTER PIPELINE
# ─────────────────────────────────────────────────────────────────────────

def apply_all_filters(
    jobs: List[Job], db: Optional[Any] = None
) -> tuple[List[Job], int, int]:
    """
    Apply all filters in sequence: notification rules → preferences.
    
    Args:
        jobs: List of jobs to filter
        db: JobDatabase instance
    
    Returns:
        Tuple of (filtered_jobs, notification_filtered_count, preference_filtered_count)
    """
    # Step 1: Apply notification filters
    after_notification = apply_notification_filters(jobs, db)
    notification_removed = len(jobs) - len(after_notification)

    # Step 2: Apply preference filters
    after_preferences = apply_preference_filters(after_notification, db)
    preference_removed = len(after_notification) - len(after_preferences)

    total_jobs = len(jobs)
    total_removed = notification_removed + preference_removed
    total_passed = total_jobs - total_removed

    logger.info(
        f"[filter] 🔹 {total_passed} passed  🔸 {total_removed} removed"
    )
    
    return after_preferences, notification_removed, preference_removed
