"""The provider-port (contracts.md §7).

Handlers depend on this port, never on an HTTP client: the URL of the provider
lives only in the adapter (``app.adapters.provider_http_client``).

The port distinguishes the two failure kinds the retry rules need:

* :class:`ProviderUnavailable` - connection errors, timeouts and ``5xx``: worth
  retrying with backoff (handoff #5);
* :class:`ProviderRejected` - a ``4xx`` the provider will keep rejecting: retrying
  cannot help, so the consumer dead-letters the message instead of burning its
  budget.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.notification import OutboundMessage, ProviderReceipt


class ProviderError(RuntimeError):
    """The notification provider could not accept a message."""

    #: Whether another attempt could still succeed.
    retryable = False


class ProviderUnavailable(ProviderError):
    """The provider could not be reached, timed out, or answered ``5xx``."""

    retryable = True


class ProviderRejected(ProviderError):
    """The provider answered a ``4xx`` and would reject the same message again."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@runtime_checkable
class NotificationProvider(Protocol):
    """Deliver exactly one notification message (contracts.md §7)."""

    def send(self, message: OutboundMessage) -> ProviderReceipt:
        """Deliver ``message``; raise :class:`ProviderError` when it cannot be accepted."""


__all__ = [
    "NotificationProvider",
    "ProviderError",
    "ProviderRejected",
    "ProviderUnavailable",
]
