"""
ats/router.py — Dispatches fetch / extract calls to the correct ATS adapter.

All adapters expose the same two functions:
  fetch(company, http, schema)           -> (raw_text, [ids])
  extract_new_jobs(company, http, schema, seen_ids) -> [Job, ...]

This keeps the Poller completely ATS-agnostic.
"""
from typing import List, Tuple

from core.models import Company, ATSType, Job
from core.http_client import HttpClient

# Import each adapter module
from ats import greenhouse, ashby, workable, lever, workday


# Map ATS enum value to its module
_ADAPTERS = {
    ATSType.GREENHOUSE: greenhouse,
    ATSType.ASHBY:      ashby,
    ATSType.WORKABLE:   workable,
    ATSType.WORKDAY:    workday,
    ATSType.LEVER:      lever,
}


def get_adapter(ats_type: ATSType):
    adapter = _ADAPTERS.get(ats_type)
    if adapter is None:
        raise ValueError(f"No adapter registered for ATS type: {ats_type}")
    return adapter


def fetch(company: Company, http: HttpClient, schema: dict, disable_filter: bool = False) -> Tuple[str, List[str]]:
    """Route to the correct adapter's fetch()."""
    return get_adapter(company.ats).fetch(company, http, schema, disable_filter=disable_filter)


def extract_new_jobs(
    company: Company,
    http: HttpClient,
    schema: dict,
    seen_ids: List[str],
    disable_filter: bool = False,
) -> List[Job]:
    """Route to the correct adapter's extract_new_jobs()."""
    return get_adapter(company.ats).extract_new_jobs(company, http, schema, seen_ids, disable_filter=disable_filter)
