"""In-memory, thread-safe, idempotent message store for the provider emulator.

Design notes (contracts.md §7):

* The emulator is a *test double*, not a system of record. State lives only in
  process memory on purpose, so restarting the container resets the emulator.
* It performs **no** network I/O: no DNS lookup, no outbound socket, no
  package/service call. Everything it needs arrives in the request body.
* ``messageId`` is deterministic: ``"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]``.
  The same order and template always yield the same id, in any process, on any
  host.
* Idempotency is keyed by the ``Idempotency-Key`` header. A repeated key never
  records a second message; a repeated key carrying a *different* body is
  rejected with ``409 idempotency_conflict`` (contracts.md §1).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: Templates defined by contracts.md §7.
SUPPORTED_TEMPLATES: frozenset[str] = frozenset({"order_confirmation", "order_cancellation"})

#: Channels defined by contracts.md §7 (email is the only one in scope).
SUPPORTED_CHANNELS: frozenset[str] = frozenset({"email"})


def utc_now() -> str:
    """Return the current time as ISO-8601 UTC with a ``Z`` suffix (contracts.md §1)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_message_id(template: str, order_id: str) -> str:
    """Return the deterministic provider message id required by contracts.md §7.

    ``"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]``
    """

    digest = hashlib.sha256(f"{template}:{order_id}".encode("utf-8")).hexdigest()
    return f"msg-{digest[:16]}"


def body_fingerprint(
    *,
    channel: str,
    to: str,
    template: str,
    order_id: str,
    payload: dict[str, Any],
) -> str:
    """Return a stable fingerprint of an accepted request body.

    Used to detect "same idempotency key, different body" (``409 idempotency_conflict``).
    """

    canonical = json.dumps(
        {
            "channel": channel,
            "orderId": order_id,
            "payload": payload,
            "template": template,
            "to": to,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyConflict(RuntimeError):
    """Raised when an ``Idempotency-Key`` is replayed with a different body."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"Idempotency-Key '{idempotency_key}' was already used with a different body")
        self.idempotency_key = idempotency_key


@dataclass(frozen=True)
class StoredMessage:
    """One accepted notification message."""

    message_id: str
    order_id: str
    template: str
    to: str
    channel: str
    payload: dict[str, Any]
    received_at: str
    accepted_at: str
    idempotency_key: str
    fingerprint: str

    def accepted_body(self) -> dict[str, Any]:
        """Response body of ``POST /messages`` (contracts.md §7)."""

        return {
            "messageId": self.message_id,
            "status": "accepted",
            "acceptedAt": self.accepted_at,
        }

    def summary(self) -> dict[str, Any]:
        """One item of ``GET /messages`` (contracts.md §7)."""

        return {
            "messageId": self.message_id,
            "orderId": self.order_id,
            "template": self.template,
            "to": self.to,
            "receivedAt": self.received_at,
        }


class MessageStore:
    """Thread-safe, in-memory, idempotent message store.

    The store is intentionally tiny: a dict keyed by idempotency key, an
    append-only list of accepted messages and an index per order id.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[str, StoredMessage] = {}
        self._messages: list[StoredMessage] = []

    def accept(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        template: str,
        to: str,
        channel: str,
        payload: dict[str, Any],
    ) -> tuple[StoredMessage, bool]:
        """Record a message, or return the previously recorded one.

        Returns ``(message, replayed)``. Raises :class:`IdempotencyConflict` when
        the key was already used with a different body.
        """

        fingerprint = body_fingerprint(
            channel=channel,
            to=to,
            template=template,
            order_id=order_id,
            payload=payload,
        )
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(idempotency_key)
                return existing, True

            received_at = utc_now()
            message = StoredMessage(
                message_id=deterministic_message_id(template, order_id),
                order_id=order_id,
                template=template,
                to=to,
                channel=channel,
                payload=dict(payload),
                received_at=received_at,
                accepted_at=received_at,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            self._by_key[idempotency_key] = message
            self._messages.append(message)
            return message, False

    def messages(self, order_id: str | None = None) -> list[StoredMessage]:
        """Return accepted messages, optionally filtered by ``orderId``."""

        with self._lock:
            if order_id is None:
                return list(self._messages)
            return [message for message in self._messages if message.order_id == order_id]

    def count(self, order_id: str | None = None, template: str | None = None) -> int:
        """Count accepted messages, optionally filtered by order and template."""

        return sum(
            1
            for message in self.messages(order_id)
            if template is None or message.template == template
        )

    def reset(self) -> None:
        """Drop every message (used by tests and by an explicit operator reset)."""

        with self._lock:
            self._by_key.clear()
            self._messages.clear()
