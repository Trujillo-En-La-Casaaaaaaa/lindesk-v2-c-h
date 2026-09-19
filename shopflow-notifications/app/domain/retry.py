"""Bounded retry policy (component: retry-policy).

Both retrying layers of this service - the provider adapter (handoff #5) and the
consumer (handoff #1) - use this one policy, so "bounded" has a single definition:

* ``max_attempts`` counts the first attempt, so it is always >= 1;
* the delay grows exponentially (``base * 2**(n-1)``) and is capped by
  ``max_delay_seconds``, so a long outage can never turn into a hot loop.

Pure domain code: no clock, no sleeping, no I/O. The caller decides when to sleep.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """A bounded exponential backoff schedule."""

    max_attempts: int
    base_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must not be negative")

    def should_retry(self, attempts_made: int) -> bool:
        """Return ``True`` when another attempt is still within the bound."""

        return attempts_made < self.max_attempts

    def delay_for(self, attempts_made: int) -> float:
        """Return the backoff to wait before the attempt following ``attempts_made``."""

        if attempts_made < 1:
            raise ValueError("attempts_made must be at least 1")
        exponential = self.base_delay_seconds * float(2 ** (attempts_made - 1))
        return float(min(exponential, self.max_delay_seconds))

    def delay_milliseconds(self, attempts_made: int) -> int:
        """Return :meth:`delay_for` in whole milliseconds (the broker's TTL unit)."""

        return int(round(self.delay_for(attempts_made) * 1000))

    def attempts_left(self, attempts_made: int) -> int:
        """Return how many attempts remain after ``attempts_made``."""

        return max(0, self.max_attempts - attempts_made)


__all__ = ["RetryPolicy"]
