"""Shared helpers of the integration suite."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

RESERVE = "/inventory/reservations"
RELEASE = "/inventory/releases"


def reservation_body(order_id: str, *lines: tuple[str, int]) -> dict[str, Any]:
    return {
        "orderId": order_id,
        "lines": [{"productId": product_id, "quantity": quantity} for product_id, quantity in lines],
    }


def release_body(order_id: str, *lines: tuple[str, int]) -> dict[str, Any]:
    return {
        "orderId": order_id,
        "lines": [{"productId": product_id, "quantity": quantity} for product_id, quantity in lines],
    }


def reserve_headers(order_id: str) -> dict[str, str]:
    return {"Idempotency-Key": f"order:{order_id}:reserve"}


def release_headers(order_id: str) -> dict[str, str]:
    return {"Idempotency-Key": f"order:{order_id}:release"}


def quantity_from(body: dict[str, Any], product_id: str) -> int:
    for item in body["items"]:
        if item["productId"] == product_id:
            return int(item["availableQuantity"])
    raise AssertionError(f"{product_id} is missing from the catalogue response")


def stock_of(client: httpx.Client, product_id: str) -> int:
    response = client.get(f"/products/{product_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["availableQuantity"])


def fire_concurrently(calls: Sequence[Callable[[], Any]]) -> list[Any]:
    """Run every call at (almost) the same moment and return the results in order."""

    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=30)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))
