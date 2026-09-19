"""HTTP adapter for the inventory port (component: ``inventory-http-client``).

Talks to the consumed ``shopflow-inventory`` interface of contracts.md §4:

* ``GET /products/{productId}`` - the snapshot copied into an order line;
* ``POST /inventory/availability`` - the read-only check made before an order is
  created;
* ``POST /inventory/reservations`` with ``Idempotency-Key: order:{orderId}:reserve``;
* ``POST /inventory/releases`` with ``Idempotency-Key: order:{orderId}:release``.

The idempotency keys are derived from the order id here, not supplied by the
caller, so a retry after a crash or an ambiguous timeout always replays the same
command and the inventory can recognise it as one logical operation.

Transport failures (timeout, connection error, malformed body, unexpected status)
are reported as ``inventory_unavailable``; the documented business answers are
mapped onto the domain errors of contracts.md §12.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.domain.errors import (
    IdempotencyConflict,
    InsufficientStock,
    InventoryUnavailable,
    NotFound,
    UnknownProduct,
)
from app.domain.order import RequestedLine, as_money
from app.ports.inventory import (
    AvailabilityItem,
    AvailabilityResult,
    InventoryProduct,
    ReleaseResult,
    ReservationResult,
    release_key,
    reservation_key,
)

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
LIVENESS_PATH = "/health"


class InventoryHttpClient:
    """Implements :class:`app.ports.inventory.InventoryPort` over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            transport=transport,
            headers={"Accept": JSON_CONTENT_TYPE},
        )

    # -- port surface -------------------------------------------------------
    def get_product(self, product_id: str, *, timeout_seconds: float | None = None) -> InventoryProduct:
        response = self._get(f"/products/{product_id}", timeout_seconds=timeout_seconds)
        status, body = self._interpret(response, path=f"/products/{product_id}", product_id=product_id)
        if status == 404:
            raise UnknownProduct(product_id)
        return InventoryProduct(
            product_id=_text(body.get("productId"), fallback=product_id),
            sku=_required_text(body, "sku", path=f"/products/{product_id}"),
            name=_required_text(body, "name", path=f"/products/{product_id}"),
            price=as_money(_required(body, "price", path=f"/products/{product_id}")),
        )

    def check_availability(
        self,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> AvailabilityResult:
        path = "/inventory/availability"
        response = self._post(path, json={"lines": _lines(lines)}, timeout_seconds=timeout_seconds)
        _status, body = self._interpret(response, path=path)
        raw_items = body.get("items")
        items: list[AvailabilityItem] = []
        if isinstance(raw_items, list):
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                items.append(
                    AvailabilityItem(
                        product_id=_text(raw.get("productId")),
                        requested=int(raw.get("requested", 0) or 0),
                        available=int(raw.get("available", 0) or 0),
                        sufficient=bool(raw.get("sufficient")),
                    )
                )
        return AvailabilityResult(available=bool(body.get("available")), items=tuple(items))

    def reserve(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReservationResult:
        path = "/inventory/reservations"
        response = self._post(
            path,
            json={"orderId": order_id, "lines": _lines(lines)},
            headers={"Idempotency-Key": reservation_key(order_id)},
            timeout_seconds=timeout_seconds,
        )
        _status, body = self._interpret(response, path=path, order_id=order_id, operation="reserve")
        return ReservationResult(
            order_id=_text(body.get("orderId"), fallback=order_id),
            reserved=bool(body.get("reserved")),
            replayed=bool(body.get("replayed")),
        )

    def release(
        self,
        order_id: str,
        lines: Sequence[RequestedLine],
        *,
        timeout_seconds: float | None = None,
    ) -> ReleaseResult:
        path = "/inventory/releases"
        response = self._post(
            path,
            json={"orderId": order_id, "lines": _lines(lines)},
            headers={"Idempotency-Key": release_key(order_id)},
            timeout_seconds=timeout_seconds,
        )
        _status, body = self._interpret(response, path=path, order_id=order_id, operation="release")
        return ReleaseResult(
            order_id=_text(body.get("orderId"), fallback=order_id),
            restored=bool(body.get("restored")),
            replayed=bool(body.get("replayed")),
        )

    def is_reachable(self, *, timeout_seconds: float | None = None) -> bool:
        """``True`` when the inventory answers its liveness probe."""

        try:
            response = self._client.get(LIVENESS_PATH, timeout=self._effective_timeout(timeout_seconds))
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Close the owned HTTP client (a client injected by a caller is left alone)."""

        if self._owns_client:
            self._client.close()

    # -- transport ----------------------------------------------------------
    def _get(self, path: str, *, timeout_seconds: float | None = None) -> httpx.Response:
        return self._send("GET", path, timeout_seconds=timeout_seconds)

    def _post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        return self._send("POST", path, json=json, headers=headers, timeout_seconds=timeout_seconds)

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        timeout = self._effective_timeout(timeout_seconds)
        request_headers = {"Content-Type": JSON_CONTENT_TYPE}
        request_headers.update(headers or {})
        try:
            return self._client.request(
                method,
                path,
                json=json,
                headers=request_headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise InventoryUnavailable(
                f"The inventory service did not answer {method} {path}.",
                details={"path": path, "error": f"{type(exc).__name__}: {exc}", "timeoutSeconds": timeout},
            ) from exc

    def _effective_timeout(self, timeout_seconds: float | None) -> float:
        """Bound one call by both the configured timeout and the caller's budget."""

        if timeout_seconds is None:
            return self._timeout_seconds
        budget = float(timeout_seconds)
        if budget <= 0:
            raise InventoryUnavailable(
                "The inventory call budget of this request is exhausted.",
                details={"timeoutSeconds": budget},
            )
        return max(0.001, min(self._timeout_seconds, budget))

    def _interpret(
        self,
        response: httpx.Response,
        *,
        path: str,
        order_id: str | None = None,
        operation: str | None = None,
        product_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Map the inventory answer onto the port's contract (contracts.md §12)."""

        status = response.status_code
        body = _json_object(response)
        if status == 200:
            return status, body

        code, message, details = _error_of(body)
        if status == 404:
            if code == "unknown_product" or product_id is not None:
                raise UnknownProduct(_text(details.get("productId"), fallback=product_id or ""))
            raise NotFound(message or f"The inventory does not know {path}.", details=details)
        if status == 409 and code == "insufficient_stock":
            raise InsufficientStock(
                message or "Requested quantity exceeds available stock.",
                details=details or {"orderId": order_id},
            )
        if status == 409 and code == "idempotency_conflict":
            raise IdempotencyConflict(order_id or "", operation or "")
        raise InventoryUnavailable(
            f"The inventory service answered {status} for {path}.",
            details={"path": path, "status": status, "code": code, "message": message},
        )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise InventoryUnavailable(
            "The inventory service returned a body that is not JSON.",
            details={"status": response.status_code, "error": str(exc)},
        ) from exc
    if not isinstance(body, dict):
        raise InventoryUnavailable(
            "The inventory service returned an unexpected body shape.",
            details={"status": response.status_code, "bodyType": type(body).__name__},
        )
    return body


def _error_of(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    error = body.get("error")
    if not isinstance(error, dict):
        return "", "", {}
    details = error.get("details")
    return (
        _text(error.get("code")),
        _text(error.get("message")),
        details if isinstance(details, dict) else {},
    )


def _lines(lines: Sequence[RequestedLine]) -> list[dict[str, Any]]:
    return [{"productId": line.product_id, "quantity": line.quantity} for line in lines]


def _text(value: Any, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def _required(body: dict[str, Any], field: str, *, path: str) -> Any:
    if field not in body or body[field] is None:
        raise InventoryUnavailable(
            f"The inventory response for {path} is missing {field}.",
            details={"path": path, "field": field},
        )
    return body[field]


def _required_text(body: dict[str, Any], field: str, *, path: str) -> str:
    return _text(_required(body, field, path=path))


__all__ = ["InventoryHttpClient"]
