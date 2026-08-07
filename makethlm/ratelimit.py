"""Rate-limit detection and backoff between provider retries.

Retrying a rate-limited provider immediately just burns the remaining budget,
so a failed attempt that looks like throttling waits before the next one.
"""

from __future__ import annotations

import re

# Longest a single backoff will wait, so a run stays interruptible.
MAX_BACKOFF_SECONDS = 30.0
BASE_BACKOFF_SECONDS = 2.0

_RATE_LIMIT_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile(r"rate[ _-]?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"\bquota\b.*\bexceeded\b", re.IGNORECASE),
    re.compile(r"\boverloaded\b", re.IGNORECASE),
)


def is_rate_limited(response: str) -> bool:
    """Return whether a failed response looks like provider throttling."""
    if not response:
        return False
    return any(pattern.search(response) for pattern in _RATE_LIMIT_PATTERNS)


def rate_limit_backoff(attempt: int) -> float:
    """Return the seconds to wait before retry number *attempt* (1-based)."""
    exponent = max(attempt - 1, 0)
    return min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)
