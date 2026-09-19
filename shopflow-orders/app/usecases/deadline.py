"""Request budget shared by the write use cases.

``REQUEST_TIMEOUT_SECONDS`` (contracts.md §11) bounds *one* HTTP request of this
service, not each inventory call: a create request that already spent its budget
must stop calling the inventory instead of adding another timeout. The budget is
monotonic (a wall-clock jump can never extend or shorten it) and asks for the
remaining slice before every call.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.domain.errors import InventoryUnavailable

#: The documented default of contracts.md §11.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0


class RequestBudget:
    """The remaining time of one inbound request."""

    def __init__(
        self,
        total_seconds: float | None = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monotonic = monotonic
        self.total_seconds = None if total_seconds is None else float(total_seconds)
        self._deadline = None if self.total_seconds is None else monotonic() + self.total_seconds

    @property
    def deadline(self) -> float | None:
        return self._deadline

    def remaining(self) -> float | None:
        """Return the remaining seconds, or ``None`` when the request is unbounded.

        Raises :class:`~app.domain.errors.InventoryUnavailable` when the budget is
        already exhausted, which is exactly the ``503 inventory_unavailable``
        answer an unreachable inventory produces (the caller cannot tell the
        difference, and must not report a success it did not verify).
        """

        if self._deadline is None:
            return None
        left = self._deadline - self._monotonic()
        if left <= 0:
            raise InventoryUnavailable(
                "The inventory call budget (REQUEST_TIMEOUT_SECONDS) of this request is exhausted.",
                details={"timeoutSeconds": self.total_seconds},
            )
        return left


__all__ = ["DEFAULT_REQUEST_TIMEOUT_SECONDS", "RequestBudget"]
