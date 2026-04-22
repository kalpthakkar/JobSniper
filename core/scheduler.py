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

  Algorithm — Priority-Weighted Round-Robin Sequence:
  ─────────────────────────────────────────────────────
  1. Assign each company a weight based on priority (configurable).
  2. Pre-compute a balanced sequence using the "largest remainder" or
     "interleaved by weight" method. This produces a repeating sequence
     like: [H1, H2, H1, M1, H3, H1, H2, L1, H3, ...] where HIGH companies
     appear proportionally more.
  3. A single background dispatcher drains this sequence and submits each
     company to a ThreadPoolExecutor. When the sequence is exhausted, it
     regenerates and repeats.
  4. A global rate-limiter (min_dispatch_gap_s) prevents bursting all slots
     at once — the dispatcher sleeps between dispatches so throughput is
     smooth. This gap is auto-adjusted based on observed failure rates.

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
    Priority.HIGH: 12,
    Priority.MID:  3,
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
        base_gap_s: float = 0.1,       # default time between dispatches (seconds)
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
    priority-weighted cycle. HIGH companies appear proportionally more often.

    Algorithm: for each company, its slot count = PRIORITY_WEIGHTS[priority].
    We interleave them evenly using the "shuffle by position" method:
      - Sort all slots by their fractional position within the company's quota.
      - This ensures HIGH companies don't all cluster at the start.

    Example with 2 HIGH (weight=12) + 1 LOW (weight=1):
    Total slots = 12+12+1 = 25.
    Positions for H1: [0/12, 1/12, ..., 11/12] → [0.0, 0.083, ...]
    Positions for H2: same
    Positions for L1: [0/1] → [0.0]
    After sorting all (position, company_idx): H1 and H2 alternate first,
    L1 sneaks in at position 0 (tied — broken by priority tier then index).
    """
    enabled_companies = [c for c in companies if c.enabled]
    if not enabled_companies:
        return []

    # Build (fractional_position, priority_order, company_idx, slot_idx) tuples
    entries: List[tuple] = []
    for idx, company in enumerate(enabled_companies):
        w = PRIORITY_WEIGHTS[company.priority]
        tier_order = {Priority.HIGH: 0, Priority.MID: 1, Priority.LOW: 2}[company.priority]
        for slot in range(w):
            frac = slot / w
            entries.append((frac, tier_order, idx, slot))

    # Sort: primary=fractional_position, secondary=priority tier (HIGH first), tertiary=idx
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    return [e[2] for e in entries]


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

        CRITICAL: Lock is acquired and released once per candidate — NOT held
        across the entire sequence scan. This prevents worker threads calling
        record_outcome() from blocking while the dispatcher scans hundreds of
        cooldown slots.

        Old bug: `with self._lock: for _ in range(seq_len): ...` held the lock
        for the full scan duration. With a large seq_len (e.g. 800 slots for 60
        HIGH-priority companies) and workers trying to record_outcome() after
        every HTTP call, lock contention caused visible pauses.
        """
        seq_len = len(self._sequence)
        if seq_len == 0:
            return None

        now = time.monotonic()

        for _ in range(seq_len):
            # Acquire lock only to advance pointer + read one state
            with self._lock:
                pos   = self._seq_pos % seq_len
                idx   = self._sequence[pos]
                state = self._states[idx]

                self._seq_pos += 1
                if self._seq_pos >= seq_len:
                    self._seq_pos = 0
                    self._cycle_count += 1
                    if self._cycle_count % 10 == 0:
                        self._log_stats()
                        if self._callback:
                            self._callback()

                ready = state.is_ready(now)
            # Lock released here — record_outcome() can proceed in parallel

            if ready:
                return state

        return None   # all companies in cooldown — caller should sleep briefly

    def soonest_ready_in(self) -> float:
        """
        Returns seconds until the soonest company exits its personal cooldown.
        Used by dispatcher to sleep precisely instead of busy-polling.
        Returns 0.0 if any company is already ready.
        """
        now = time.monotonic()
        with self._lock:
            if not self._states:
                return 0.0
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
