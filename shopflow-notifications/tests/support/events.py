"""Builders of contracts.md §8 order-event envelopes.

The identifiers are derived with :func:`uuid.uuid5`, so a test that asks for the
same seed twice gets the same order id and event id - which is exactly what the
duplicate-delivery tests need to be reproducible.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.domain.notification import CANCELLED_EVENT_TYPE, CONFIRMED_EVENT_TYPE

#: ``p-1001`` style ids are the seeded product ids of contracts.md §10.
DEFAULT_LINES: tuple[tuple[str, int, str], ...] = (
    ("p-1001", 3, "19.99"),
    ("p-1002", 1, "12.50"),
)


def deterministic_uuid(seed: str) -> str:
    """Return a stable UUID-format string for a seed."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"shopflow:{seed}"))


def order_id(seed: str) -> str:
    return deterministic_uuid(f"order:{seed}")


def event_id(seed: str) -> str:
    return deterministic_uuid(f"event:{seed}")


def line(product_id: str, quantity: int, unit_price: str) -> dict[str, Any]:
    return {"productId": product_id, "quantity": quantity, "unitPrice": float(unit_price)}


def lines(*triples: tuple[str, int, str]) -> list[dict[str, Any]]:
    return [line(*triple) for triple in (triples or DEFAULT_LINES)]


def order_confirmed(
    *,
    order: str,
    event: str,
    customer_id: str = "cust-smoke",
    occurred_at: str = "2026-09-18T12:00:00Z",
    correlation_id: str = "corr-smoke",
    event_lines: list[dict[str, Any]] | None = None,
    event_version: int = 1,
) -> dict[str, Any]:
    """One ``order.confirmed`` envelope (contracts.md §8)."""

    return {
        "eventId": event,
        "eventType": CONFIRMED_EVENT_TYPE,
        "eventVersion": event_version,
        "occurredAt": occurred_at,
        "correlationId": correlation_id,
        "orderId": order,
        "customerId": customer_id,
        "lines": lines() if event_lines is None else event_lines,
        "reason": None,
    }


def order_cancelled(
    *,
    order: str,
    event: str,
    reason: str = "customer changed their mind",
    customer_id: str = "cust-smoke",
    occurred_at: str = "2026-09-18T12:05:00Z",
    correlation_id: str = "corr-smoke",
    event_lines: list[dict[str, Any]] | None = None,
    event_version: int = 1,
) -> dict[str, Any]:
    """One ``order.cancelled`` envelope (contracts.md §8)."""

    return {
        "eventId": event,
        "eventType": CANCELLED_EVENT_TYPE,
        "eventVersion": event_version,
        "occurredAt": occurred_at,
        "correlationId": correlation_id,
        "orderId": order,
        "customerId": customer_id,
        "lines": lines() if event_lines is None else event_lines,
        "reason": reason,
    }


__all__ = [
    "DEFAULT_LINES",
    "deterministic_uuid",
    "event_id",
    "line",
    "lines",
    "order_cancelled",
    "order_confirmed",
    "order_id",
]
