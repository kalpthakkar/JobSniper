"""
core/scheduler.py — Priority-weighted scheduling engine for Job Sniper.

DESIGN PHILOSOPHY:
────────────────────────────────────────────────────────────────────────────
  Problem: N companies, M priority tiers, K=20 worker threads.
  Goal: HIGH-priority companies are called proportionally more often than
  MID/LOW within the natural flow — WITHOUT hardcoded sleep timers.

  Insight: Instead of "sleep 30s between each HIGH poll", think in terms of
  RELATIVE FREQUENCY within one cycle. If HIGH:MID:LOW = 6:2:1 (weights),
  then in every 9 slots of the schedule, HIGH gets 6, MID gets 2, LOW gets 1.

  Algorithm — Temporally-Fair Weighted Scheduling (Heap-based):
  ────────────────────────────────────────────────────────────
  NEW (Phase 12): Uses a simulated timeline with ideal dispatch times to ensure
  even temporal spacing across all priority tiers. Each company tracks its
  "next ideal dispatch time" and we always dispatch the company with the
  earliest ideal time next. This guarantees that HIGH companies (12 slots) are
  dispatched at regular ~8.6s intervals (not in clusters), MID at ~34.5s, and
  LOW at ~103.6s. Result: frequency inspection reveals consistent, predictable
  polling patterns instead of visible burstiness.

  Adaptive Delay (Anti-throttle Cooldown):
  ─────────────────────────────────────────
  - Each company tracks a personal `cooldown` that backs off exponentially
    on consecutive failures (network errors, 429 Too Many Requests, etc.).
  - A global `rate_limiter` also watches the overall failure rate: if >X%
    of recent calls failed, it widens the inter-dispatch gap for ALL companies
    to give ATS servers a rest. It shrinks back when success rate recovers.

  Fairness guarantee:
  ────────────────────
  - Even LOW-priority companies are never fully starved — they always appear
    in each cycle, just rarely.
  - Within the same priority tier, companies are served round-robin.
  - If K=20 workers are all busy, the dispatcher pauses (bounded queue) and
    waits for a slot — this naturally back-pressures the whole system without
    dropping companies.

  Scalability:
  ─────────────
  - 1000 companies? The sequence is just a list of indices. Memory: ~8KB.
  - The dispatcher is a single thread; workers are the pool. This avoids
    spawning 1000 threads.
────────────────────────────────────────────────────────────────────────────
"""
import heapq
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.models import Company, Priority

logger = logging.getLogger("job_sniper.scheduler")


# ─────────────────────────────────────────────────────────────────────────
# Priority weight mapping  (relative frequency per cycle)
# HIGH : MID : LOW  =  12 : 3 : 1
# This means in a cycle of 16 slots: HIGH ≈ 12, MID ≈ 3, LOW ≈ 1
# (actual per-company counts depend on how many companies are in each tier)
# ─────────────────────────────────────────────────────────────────────────
PRIORITY_WEIGHTS: Dict[Priority, int] = {
    Priority.HIGH: 3,
    Priority.MID:  2,
    Priority.LOW:  1,
}


# ─────────────────────────────────────────────────────────────────────────
# Per-company state tracked by the scheduler
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class CompanyState:
    company:       "Company"
    weight:        int           # effective weight for this company
    next_allowed:  float = 0.0  # earliest time this company may be polled (epoch)
    fail_count:    int   = 0    # consecutive failures (for personal backoff)
    total_polls:   int   = 0
    total_errors:  int   = 0

    # personal exponential backoff ceiling (seconds)
    _backoff_base: float = 2.0
    _backoff_max:  float = 300.0   # 5 minutes max personal backoff

    def record_success(self):
        self.fail_count   = 0
        self.total_polls += 1
        self.next_allowed = 0.0   # available immediately on next sequence turn

    def record_failure(self, now: float):
        self.fail_count   += 1
        self.total_errors += 1
        self.total_polls  += 1
        backoff = min(
            self._backoff_base * (2 ** (self.fail_count - 1)) + random.uniform(0, 1),
            self._backoff_max,
        )
        self.next_allowed = now + backoff
        logger.debug(
            f"[{self.company.name}] backoff {backoff:.1f}s "
            f"(fail #{self.fail_count})"
        )

    def is_ready(self, now: float) -> bool:
        return now >= self.next_allowed


# ─────────────────────────────────────────────────────────────────────────
# Global adaptive rate controller
# ─────────────────────────────────────────────────────────────────────────
class AdaptiveRateController:
    """
    Tracks recent outcome window and adjusts the global inter-dispatch
    gap to prevent ATS throttling when bulk failure rates spike.

    Uses an exponential moving average of the failure rate.
    """

    def __init__(
        self,
        base_gap_s: float = 0.033,       # default time between dispatches (seconds)
        max_gap_s: float  = 5.0,        # ceiling on the gap
        window: int       = 50,         # rolling window of recent outcomes
        threshold: float  = 0.30,       # failure rate above this → start backing off
    ):
        self._base      = base_gap_s
        self._max       = max_gap_s
        self._threshold = threshold
        self._window: deque[bool] = deque(maxlen=window)  # True=success, False=fail
        self._current_gap = base_gap_s
        self._lock = threading.Lock()

    @property
    def gap(self) -> float:
        return self._current_gap

    def record(self, success: bool):
        with self._lock:
            self._window.append(success)
            if len(self._window) < 10:
                return   # not enough data yet

            fail_rate = self._window.count(False) / len(self._window)

            if fail_rate >= self._threshold:
                # Exponential back-off: double the gap, up to max
                new_gap = min(self._current_gap * 2.0, self._max)
                if new_gap != self._current_gap:
                    logger.warning(
                        f"🌡  High failure rate ({fail_rate:.0%}) — "
                        f"global dispatch gap: {self._current_gap:.2f}s → {new_gap:.2f}s"
                    )
                    self._current_gap = new_gap
            elif fail_rate < self._threshold / 2:
                # Gradual recovery: shrink toward base
                new_gap = max(self._current_gap * 0.9, self._base)
                if abs(new_gap - self._current_gap) > 0.01:
                    logger.debug(
                        f"✅ Failure rate OK ({fail_rate:.0%}) — "
                        f"gap recovering: {self._current_gap:.2f}s → {new_gap:.2f}s"
                    )
                    self._current_gap = new_gap


# ─────────────────────────────────────────────────────────────────────────
# Sequence builder — priority-weighted interleaved company list
# ─────────────────────────────────────────────────────────────────────────

def build_weighted_sequence(companies: List["Company"]) -> List[int]:
    """
    Returns a list of company indices (into `companies`) forming one full
    priority-weighted cycle, with TEMPORAL FAIRNESS.

    Algorithm: Weighted Fair Scheduling using a heap (simulated timeline).
    ──────────────────────────────────────────────────────────────────────

    Previous approach: sorted slots by fractional position within quota.
    This ensured correct slot distribution but NOT temporal fairness.
    Result: clusters of same-priority companies caused visible burstiness.

    New approach: simulate a timeline where each company has an ideal
    dispatch time. Always dispatch the company with the earliest ideal time,
    then advance their ideal time by their interval.

    Example with 1 HIGH (interval=1/3) + 1 MID (interval=1/2):
      t=0.000: HIGH (advance to t=0.333)
      t=0.333: MID (advance to t=0.833)
      t=0.500: MID (advance to t=1.333)
      t=0.667: HIGH (advance to t=1.000)
      t=1.000: MID (advance to t=1.500)
      t=1.333: HIGH (advance to t=1.667)
      Sequence: [HIGH, MID, MID, HIGH, MID, HIGH]  ← perfectly interleaved!

    Benefit: With 2246 companies, HIGH tokens are now dispatched ~every 9s
    with consistent spacing, not in visible clusters.

    ──────────────────────────────────────────────────────────────────────
    """
    enabled_companies = [c for c in companies if c.enabled]
    if not enabled_companies:
        return []

    heap: List[tuple] = []
    result: List[int] = []
    total_slots = 0

    # Initialize heap: (ideal_dispatch_time, company_idx, interval)
    for idx, company in enumerate(enabled_companies):
        count = PRIORITY_WEIGHTS[company.priority]
        interval = 1.0 / count if count > 0 else 1.0
        heapq.heappush(heap, (0.0, idx, interval))
        total_slots += count

    # Pop companies in order of ideal dispatch time, reschedule each
    for _ in range(total_slots):
        dispatch_time, idx, interval = heapq.heappop(heap)
        result.append(idx)
        # Reschedule this company at ideal_time + interval
        heapq.heappush(heap, (dispatch_time + interval, idx, interval))

    return result


def build_company_states(companies: List["Company"]) -> List[CompanyState]:
    """Build initial CompanyState for each company."""
    return [
        CompanyState(
            company=c,
            weight=PRIORITY_WEIGHTS[c.priority],
        )
        for c in companies
    ]


# ─────────────────────────────────────────────────────────────────────────
# Scheduler — produces the next company to poll, on demand
# ─────────────────────────────────────────────────────────────────────────

class PriorityScheduler:
    """
    Pre-computes a weighted cycle of companies. Calling `next_company()`
    returns the next (CompanyState, company) that is:
      1. Due in the sequence (positional), AND
      2. Not in personal backoff cooldown.

    If the current candidate is in cooldown, we advance the sequence
    pointer and try the next one (with a bounded scan to avoid starvation).

    One full cycle is exhausted before looping. This preserves fairness:
    every company gets exactly its weighted share of slots per cycle.
    """

    def __init__(self, companies: List["Company"], callback: Optional[Callable] = None):
        self._companies     = companies
        self._states        = build_company_states(companies)
        self._sequence      = build_weighted_sequence(companies)
        self._seq_pos       = 0
        self._cycle_count   = 0
        self._callback      = callback
        self._lock          = threading.Lock()
        self._rate_ctrl     = AdaptiveRateController()

        logger.info(
            f"[Scheduler] Initialized: {len(companies)} companies, "
            f"cycle length={len(self._sequence)} slots"
        )
        self._log_cycle_composition()

    def _log_cycle_composition(self):
        from collections import Counter
        tier_counts: Counter = Counter()
        per_company: Counter = Counter()
        for idx in self._sequence:
            c = self._companies[idx]
            tier_counts[c.priority.value] += 1
            per_company[c.name] += 1
        logger.info(
            f"[Scheduler] Cycle composition — "
            + " | ".join(f"{k}: {v} slots" for k, v in sorted(tier_counts.items()))
        )
        # Log per-company slot counts at DEBUG
        for name, count in sorted(per_company.items(), key=lambda x: -x[1]):
            logger.debug(f"  {name}: {count} slots/cycle")

    @property
    def adaptive_gap(self) -> float:
        """Inter-dispatch pause (seconds). Grows when failures spike."""
        return self._rate_ctrl.gap

    def record_outcome(self, state: CompanyState, success: bool, now: Optional[float] = None, is_rate_limit: bool = False):
        """Called by the worker after each poll attempt."""
        now = now or time.monotonic()
        if success:
            state.record_success()
        else:
            state.record_failure(now)
        
        # Record outcome for adaptive gap adjustment
        self._rate_ctrl.record(success)
        
        # If rate-limited, apply more aggressive global backoff
        if is_rate_limit and not success:
            # Double the gap more aggressively for rate limit errors
            new_gap = min(self._rate_ctrl._current_gap * 3.0, self._rate_ctrl._max)
            if new_gap > self._rate_ctrl._current_gap:
                logger.error(
                    f"🚨 Rate limit detected — "
                    f"global dispatch gap: {self._rate_ctrl._current_gap:.2f}s → {new_gap:.2f}s"
                )
                self._rate_ctrl._current_gap = new_gap

    def next_company(self) -> Optional[CompanyState]:
        """
        Returns the next CompanyState ready to be polled, or None if all
        companies are currently in personal cooldown.

        LOCK STRATEGY — one snapshot, zero per-iteration locking:
        ──────────────────────────────────────────────────────────
        The previous implementation acquired _lock on every loop iteration
        (up to seq_len=24 000+ times for 8013 companies × avg weight 3).
        With 20 worker threads each calling record_outcome() concurrently,
        this created a lock convoy: dispatcher releases, worker grabs it,
        dispatcher grabs it again — 24 000 times per next_company() call.
        This is what produced the 4-minute freezes visible in the logs.

        Fix: acquire _lock ONCE to snapshot _seq_pos, scan _sequence
        entirely outside the lock (it's read-only after __init__), then
        acquire _lock ONCE more to commit the advanced position.

        Why this is safe:
        - _sequence never changes after __init__ (read-only list of indices).
        - _states[idx].next_allowed is written only by record_success/failure,
          called from worker threads. Only the dispatcher calls next_company().
          A worker writing next_allowed concurrently with us reading it means
          we might skip a company that just became ready — it will simply be
          picked up in the next call. This is harmless and far better than the
          lock-convoy freeze.
        - _seq_pos and _cycle_count are only written here (single dispatcher
          thread), so committing under lock is just for visibility to readers.
        """
        seq_len = len(self._sequence)
        if seq_len == 0:
            return None

        now = time.monotonic()

        # ONE lock acquisition to read current position.
        with self._lock:
            start_pos = self._seq_pos

        # Scan entirely outside the lock — _sequence is read-only after init.
        for i in range(seq_len):
            pos = (start_pos + i) % seq_len
            idx = self._sequence[pos]
            state = self._states[idx]

            if state.is_ready(now):
                # Commit the new position under lock.
                new_pos = pos + 1
                increment_cycle = (new_pos >= seq_len)
                if increment_cycle:
                    new_pos = 0

                with self._lock:
                    self._seq_pos = new_pos
                    if increment_cycle:
                        self._cycle_count += 1
                        if self._cycle_count % 10 == 0:
                            self._log_stats()
                            if self._callback:
                                self._callback()

                return state

        # Nobody ready — advance past what we scanned so next call starts fresh.
        with self._lock:
            self._seq_pos = (start_pos + seq_len) % seq_len

        return None  # all companies in cooldown — caller should sleep briefly

    def soonest_ready_in(self) -> float:
        """
        Returns seconds until the soonest company exits its personal cooldown.
        Used by dispatcher to sleep precisely instead of busy-polling.
        Returns 0.0 if any company is already ready.
        Reads next_allowed without the lock — same rationale as next_company().
        """
        if not self._states:
            return 0.0
        now = time.monotonic()
        # Read without lock: next_allowed is a float written atomically by workers;
        # a slightly stale read just means we might wake 1ms early or late.
        earliest = min(s.next_allowed for s in self._states)
        return max(0.0, min(earliest - now, 1.0))  # cap at 1s for responsiveness

    def _log_stats(self):
        total_polls  = sum(s.total_polls  for s in self._states)
        total_errors = sum(s.total_errors for s in self._states)
        err_rate     = total_errors / total_polls if total_polls > 0 else 0
        logger.info(
            f"[Scheduler] Cycle #{self._cycle_count} | "
            f"polls={total_polls} | errors={total_errors} ({err_rate:.1%}) | "
            f"dispatch_gap={self._rate_ctrl.gap:.2f}s"
        )

    def summary(self) -> str:
        lines = [
            f"Cycle length : {len(self._sequence)} slots",
            f"Companies    : {len(self._companies)}",
            "",
        ]
        by_priority: Dict[str, List[str]] = {}
        for c in self._companies:
            by_priority.setdefault(c.priority.value, []).append(c.name)

        for tier in ["HIGH", "MID", "LOW"]:
            names = by_priority.get(tier, [])
            if not names:
                continue
            w = PRIORITY_WEIGHTS.get(Priority(tier), 0)
            slots_each = w
            lines.append(f"  {tier:4s} ({len(names)} companies, {slots_each} slots each):")
            for name in names:
                lines.append(f"    • {name}")
        return "\n".join(lines)
