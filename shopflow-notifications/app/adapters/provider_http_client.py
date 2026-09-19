"""HTTP adapter of the provider-port (component: provider-http-client, handoff #5).

The **only** place in this repository that knows the provider URL: it is built
from ``PROVIDER_BASE_URL`` (contracts.md §11) by the composition root and the
handlers only ever see the port.

Implements exactly the call of contracts.md §7:

* ``POST {PROVIDER_BASE_URL}/messages`` with
  ``{channel: "email", to, template, orderId, payload}``;
* the required ``Idempotency-Key: order:{orderId}:{template}`` header, which is
  what makes a retry after an ambiguous failure return the original ``messageId``
  instead of recording a second message;
* ``X-Correlation-Id`` forwarded from the event envelope (contracts.md §1);
* bounded retry with exponential backoff for connection errors, timeouts and
  ``5xx``. A ``4xx`` is permanent: retrying cannot help, so it is reported as
  :class:`ProviderRejected` and the consumer dead-letters the message instead of
  looping. A ``409 idempotency_conflict`` is an idempotent replay of the same
  logical notification, so the deterministic ``messageId`` of contracts.md §7 is
  reported and no second message exists.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.domain.notification import OutboundMessage, ProviderReceipt
from app.domain.retry import RetryPolicy
from app.ports.provider import ProviderRejected, ProviderUnavailable

LOGGER = logging.getLogger("shopflow.notifications.provider")

PROVIDER_MESSAGES_PATH = "/messages"
PROVIDER_READY_PATH = "/ready"
IDEMPOTENCY_HEADER = "Idempotency-Key"
CORRELATION_HEADER = "X-Correlation-Id"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: contracts.md §7: ``channel`` is one of ``email``; this service owns no other channel.
CHANNEL_EMAIL = "email"

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 5.0


def deterministic_message_id(template: str, order_id: str) -> str:
    """contracts.md §7: ``"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]``."""

    digest = hashlib.sha256(f"{template}:{order_id}".encode()).hexdigest()
    return f"msg-{digest[:16]}"


def message_body(message: OutboundMessage) -> dict[str, Any]:
    """The exact body of ``POST /messages`` (contracts.md §7)."""

    return {
        "channel": CHANNEL_EMAIL,
        "to": message.recipient,
        "template": message.template,
        "orderId": message.order_id,
        "payload": message.payload,
    }


def provider_is_reachable(
    base_url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> bool:
    """Return ``True`` when the provider answers its readiness route.

    The provider emulator of ``shopflow-infra`` publishes ``GET /ready`` next to
    the §7 routes; the probe only reads it and never performs business work.
    """

    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout_seconds, follow_redirects=False)
    try:
        response = http.get(f"{base_url.rstrip('/')}{PROVIDER_READY_PATH}", timeout=timeout_seconds)
    except httpx.HTTPError:
        return False
    finally:
        if owns_client:
            http.close()
    return 200 <= response.status_code < 300


class HttpNotificationProvider:
    """Conversation with the notification provider over HTTP/JSON."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._policy = RetryPolicy(
            max_attempts=int(max_attempts),
            base_delay_seconds=float(backoff_seconds),
            max_delay_seconds=float(backoff_max_seconds),
        )
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ #
    # port surface
    # ------------------------------------------------------------------ #
    def send(self, message: OutboundMessage) -> ProviderReceipt:
        """Deliver one message (contracts.md §7), retrying the retryable failures."""

        body = message_body(message)
        headers = {IDEMPOTENCY_HEADER: message.idempotency_key}
        if message.correlation_id:
            headers[CORRELATION_HEADER] = message.correlation_id

        attempts = 0
        last_error = "no attempt was made"
        while True:
            attempts += 1
            try:
                response = self._client.post(
                    f"{self._base_url}{PROVIDER_MESSAGES_PATH}",
                    json=body,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= response.status_code < 300:
                    try:
                        return self._receipt_of(response, body, message)
                    except ProviderUnavailable as exc:
                        # A success status without the §7 body: the provider may still
                        # have recorded the message, and the Idempotency-Key makes
                        # another attempt safe, so this stays inside the retry budget.
                        last_error = str(exc)
                elif response.status_code == 409:
                    # contracts.md §7: a repeated Idempotency-Key never records a
                    # second message. The logical effect already holds, and the id
                    # is deterministic, so the notification is reported as accepted.
                    LOGGER.warning(
                        "provider reported an idempotency replay for %s (orderId=%s)",
                        message.idempotency_key,
                        message.order_id,
                    )
                    return ProviderReceipt(
                        message_id=deterministic_message_id(message.template, message.order_id),
                        status="accepted",
                        accepted_at=None,
                    )
                elif response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                else:
                    raise ProviderRejected(
                        f"the provider rejected the message: HTTP {response.status_code}",
                        status_code=response.status_code,
                        body=response.text[:200],
                    )

            if not self._policy.should_retry(attempts):
                raise ProviderUnavailable(
                    f"the provider did not accept the message after {attempts} attempt(s): {last_error}"
                )
            self._sleep(self._policy.delay_for(attempts))

    def is_reachable(self) -> bool:
        """Probe the provider readiness route (used by ``/ready``)."""

        return provider_is_reachable(
            self._base_url,
            timeout_seconds=self._timeout_seconds,
            client=self._client,
        )

    def close(self) -> None:
        """Close the underlying HTTP client when this adapter owns it."""

        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _receipt_of(self, response: httpx.Response, body: dict[str, Any], message: OutboundMessage) -> ProviderReceipt:
        try:
            payload = response.json()
        except ValueError as exc:
            # A success status with a body that is not the §7 object: the provider
            # may still have recorded the message, and the Idempotency-Key makes a
            # retry safe, so this is reported as retryable rather than permanent.
            raise ProviderUnavailable(f"the provider answered HTTP {response.status_code} without a JSON body") from exc

        message_id = payload.get("messageId") if isinstance(payload, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise ProviderUnavailable(
                f"the provider answered HTTP {response.status_code} without a messageId "
                f"(orderId={message.order_id}, template={message.template})"
            )
        status = payload.get("status")
        accepted_at = payload.get("acceptedAt")
        return ProviderReceipt(
            message_id=message_id.strip(),
            status=status.strip() if isinstance(status, str) and status.strip() else "accepted",
            accepted_at=accepted_at.strip() if isinstance(accepted_at, str) and accepted_at.strip() else None,
        )


__all__ = [
    "CHANNEL_EMAIL",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpNotificationProvider",
    "deterministic_message_id",
    "message_body",
    "provider_is_reachable",
]
