"""Contract-faithful ``shopflow-orders`` HTTP service for the gateway suites.

It implements the orders API of contracts.md §5 - order creation (which reserves
stock in ``shopflow-inventory``), order reads, cancellation and shipping - with
the owned SQL schema of contracts.md §9. It is backed by SQLite instead of
PostgreSQL so the suite needs no database server; the observable HTTP behaviour
(status codes, error codes, order view shape, cancel/ship state machine) follows
the contract.

Deliberate harness simplifications (documented, never visible to the gateway's
own contract): no broker publishing for order events, and an order that failed
because inventory was unreachable is recorded as ``FAILED`` without line prices.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware import Middleware

from tests.support.upstreams.harness import (
    FaultController,
    InstrumentationMiddleware,
    RequestLog,
    iso_now,
    upstream_error,
)
from tests.support.upstreams.inventory import UpstreamFailure

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('CREATED','CONFIRMED','FAILED','SHIPPED','CANCELLED')),
  inventory_release_state TEXT NOT NULL DEFAULT 'NONE' CHECK (inventory_release_state IN ('NONE','PENDING','RELEASED')),
  total NUMERIC NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  confirmed_at TEXT,
  shipped_at TEXT,
  cancelled_at TEXT,
  cancellation_reason TEXT
);
CREATE TABLE IF NOT EXISTS order_lines (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  product_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_price NUMERIC NOT NULL
);
"""

MAX_CANCELLATION_REASON_LENGTH = 200


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productId: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0)


class OrderCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customerId: str = Field(..., min_length=1)
    lines: list[LineIn] = Field(..., min_length=1)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class OrdersStore:
    """SQLite-backed order state with the ``shopflow-orders`` HTTP behaviour."""

    def __init__(self, db_path: str, inventory_base_url: str, timeout_seconds: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._inventory_base_url = inventory_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def http(self) -> httpx.AsyncClient:
        """Return the shared client used to call ``shopflow-inventory``."""

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def reset(self) -> None:
        """Drop all orders (used between tests when a stack is reused)."""

        with self._lock:
            self._conn.execute("DELETE FROM order_lines")
            self._conn.execute("DELETE FROM orders")

    # ---------------------------------------------------------------- creation
    async def create(
        self,
        customer_id: str,
        lines: list[dict[str, Any]],
        correlation_id: str,
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        headers = {"X-Correlation-Id": correlation_id}
        client = self.http()
        availability = await self._check_availability(client, lines, headers)
        if not availability.get("available"):
            raise UpstreamFailure(
                409,
                "insufficient_stock",
                "Requested quantity exceeds available stock.",
            )
        enriched = await self._line_details(client, lines, headers)
        await self._reserve(client, order_id, lines, headers)
        return await asyncio.to_thread(self._persist, order_id, customer_id, "CONFIRMED", enriched)

    async def _check_availability(
        self,
        client: httpx.AsyncClient,
        lines: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = await client.post(
                f"{self._inventory_base_url}/inventory/availability",
                json={"lines": lines},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise UpstreamFailure(503, "inventory_unavailable", "Inventory service unreachable.") from exc
        if response.status_code == 404:
            raise UpstreamFailure(404, "unknown_product", "A referenced product does not exist.")
        if response.status_code >= 400:
            raise UpstreamFailure(400, "validation_error", "Inventory rejected the availability request.")
        return response.json()

    async def _line_details(
        self,
        client: httpx.AsyncClient,
        lines: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for line in lines:
            try:
                response = await client.get(f"{self._inventory_base_url}/products/{line['productId']}", headers=headers)
            except httpx.HTTPError as exc:
                raise UpstreamFailure(503, "inventory_unavailable", "Inventory service unreachable.") from exc
            if response.status_code == 404:
                raise UpstreamFailure(404, "unknown_product", f"Unknown product {line['productId']}.")
            if response.status_code >= 400:
                raise UpstreamFailure(400, "validation_error", "Inventory rejected the product read.")
            product = response.json()
            enriched.append(
                {
                    "productId": line["productId"],
                    "quantity": int(line["quantity"]),
                    "sku": product["sku"],
                    "name": product["name"],
                    "unitPrice": float(product["price"]),
                }
            )
        return enriched

    async def _reserve(
        self,
        client: httpx.AsyncClient,
        order_id: str,
        lines: list[dict[str, Any]],
        headers: dict[str, str],
    ) -> None:
        try:
            response = await client.post(
                f"{self._inventory_base_url}/inventory/reservations",
                json={"orderId": order_id, "lines": lines},
                headers={**headers, "Idempotency-Key": f"order:{order_id}:reserve"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamFailure(503, "inventory_unavailable", "Inventory service unreachable.") from exc
        if response.status_code == 409:
            body = response.json().get("error", {})
            raise UpstreamFailure(409, str(body.get("code", "insufficient_stock")), "Reservation rejected.")
        if response.status_code >= 400:
            raise UpstreamFailure(400, "validation_error", "Inventory rejected the reservation request.")

    def _persist(
        self,
        order_id: str,
        customer_id: str,
        status: str,
        lines: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = iso_now()
        total = round(sum(float(line["unitPrice"]) * int(line["quantity"]) for line in lines), 2)
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders (id, customer_id, status, inventory_release_state, total, created_at, updated_at,"
                " confirmed_at, shipped_at, cancelled_at, cancellation_reason)"
                " VALUES (?, ?, ?, 'NONE', ?, ?, ?, ?, NULL, NULL, NULL)",
                (order_id, customer_id, status, total, now, now, now if status == "CONFIRMED" else None),
            )
            for line in lines:
                self._conn.execute(
                    "INSERT INTO order_lines (id, order_id, product_id, sku, name, quantity, unit_price)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        order_id,
                        line["productId"],
                        line.get("sku", ""),
                        line.get("name", ""),
                        int(line["quantity"]),
                        float(line.get("unitPrice", 0.0)),
                    ),
                )
        return self.view(order_id)

    # ------------------------------------------------------------------- reads
    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self.view(order_id) if self._row(order_id) is not None else None

    def list_orders(self, customer_id: str, limit: int | None, offset: int | None) -> dict[str, Any]:
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM orders WHERE customer_id = ?",
                    (customer_id,),
                ).fetchone()["c"]
            )
            rows = self._conn.execute(
                "SELECT id FROM orders WHERE customer_id = ? ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (customer_id, limit if limit is not None else -1, offset or 0),
            ).fetchall()
        return {"items": [self.view(row["id"]) for row in rows], "total": total}

    def _row(self, order_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def view(self, order_id: str) -> dict[str, Any]:
        row = self._row(order_id)
        if row is None:
            raise UpstreamFailure(404, "not_found", f"Unknown order {order_id}.")
        with self._lock:
            line_rows = self._conn.execute(
                "SELECT product_id, sku, name, quantity, unit_price FROM order_lines WHERE order_id = ?"
                " ORDER BY rowid",
                (order_id,),
            ).fetchall()
        return {
            "orderId": row["id"],
            "status": row["status"],
            "customerId": row["customer_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "confirmedAt": row["confirmed_at"],
            "shippedAt": row["shipped_at"],
            "cancelledAt": row["cancelled_at"],
            "cancellationReason": row["cancellation_reason"],
            "total": float(row["total"]),
            "lines": [
                {
                    "productId": line["product_id"],
                    "sku": line["sku"],
                    "name": line["name"],
                    "quantity": int(line["quantity"]),
                    "unitPrice": float(line["unit_price"]),
                    "lineTotal": round(float(line["unit_price"]) * int(line["quantity"]), 2),
                }
                for line in line_rows
            ],
        }

    # ------------------------------------------------------------------ actions
    async def cancel(self, order_id: str, reason: str, correlation_id: str) -> dict[str, Any]:
        order = await asyncio.to_thread(self._row, order_id)
        if order is None:
            raise UpstreamFailure(404, "not_found", f"Unknown order {order_id}.")
        if not reason.strip():
            raise UpstreamFailure(400, "invalid_reason", "Cancellation reason must not be empty.")
        if len(reason) > MAX_CANCELLATION_REASON_LENGTH:
            raise UpstreamFailure(400, "invalid_reason", "Cancellation reason is too long.")
        status = str(order["status"])
        if status == "SHIPPED":
            raise UpstreamFailure(409, "order_shipped", "Cancellation rejected because the order is SHIPPED.")
        if status == "FAILED":
            raise UpstreamFailure(409, "order_not_cancellable", "Order is not in a cancellable state.")
        if status == "CANCELLED":
            return await asyncio.to_thread(self.view, order_id)

        release_state = "RELEASED"
        lines = [
            {"productId": line["product_id"], "quantity": int(line["quantity"])}
            for line in await asyncio.to_thread(self._lines, order_id)
        ]
        headers = {"X-Correlation-Id": correlation_id, "Idempotency-Key": f"order:{order_id}:release"}
        try:
            response = await self.http().post(
                f"{self._inventory_base_url}/inventory/releases",
                json={"orderId": order_id, "lines": lines},
                headers=headers,
            )
            if response.status_code >= 500:
                release_state = "PENDING"
        except httpx.HTTPError:
            # The release is retried later; the cancellation itself still commits.
            release_state = "PENDING"

        await asyncio.to_thread(self._mark_cancelled, order_id, reason, release_state)
        return await asyncio.to_thread(self.view, order_id)

    def _mark_cancelled(self, order_id: str, reason: str, release_state: str) -> None:
        now = iso_now()
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status = 'CANCELLED', cancelled_at = ?, cancellation_reason = ?,"
                " updated_at = ?, inventory_release_state = ? WHERE id = ?",
                (now, reason, now, release_state, order_id),
            )

    def ship(self, order_id: str) -> dict[str, Any]:
        order = self._row(order_id)
        if order is None:
            raise UpstreamFailure(404, "not_found", f"Unknown order {order_id}.")
        if str(order["status"]) != "CONFIRMED":
            raise UpstreamFailure(
                409,
                "invalid_status_transition",
                "Ship attempted from a status other than CONFIRMED.",
            )
        now = iso_now()
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status = 'SHIPPED', shipped_at = ?, updated_at = ? WHERE id = ?",
                (now, now, order_id),
            )
        return self.view(order_id)

    def _lines(self, order_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM order_lines WHERE order_id = ? ORDER BY rowid",
                    (order_id,),
                ).fetchall()
            )


def create_orders_app(
    db_path: str,
    *,
    inventory_base_url: str,
    log: RequestLog | None = None,
    faults: FaultController | None = None,
) -> FastAPI:
    """Build the harness orders service (contracts.md §5)."""

    request_log = log if log is not None else RequestLog()
    fault_controller = faults if faults is not None else FaultController()
    store = OrdersStore(db_path, inventory_base_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await store.aclose()

    app = FastAPI(
        title="shopflow-orders (contract-faithful test harness)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        middleware=[Middleware(InstrumentationMiddleware, log=request_log, faults=fault_controller)],
    )
    app.state.store = store
    app.state.log = request_log
    app.state.faults = fault_controller

    @app.exception_handler(UpstreamFailure)
    async def _handle_failure(request: Request, exc: UpstreamFailure) -> JSONResponse:
        return upstream_error(
            exc.status_code,
            exc.code,
            exc.message,
            request.headers.get("X-Correlation-Id", ""),
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "shopflow-orders"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"status": "ready", "service": "shopflow-orders"})

    @app.post("/orders")
    async def create_order(request: Request, body: OrderCreateBody) -> JSONResponse:
        order = await store.create(
            body.customerId,
            [line.model_dump() for line in body.lines],
            request.headers.get("X-Correlation-Id", ""),
        )
        return JSONResponse(order, status_code=201)

    @app.get("/orders")
    async def list_orders(customerId: str, limit: int | None = None, offset: int | None = None) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(store.list_orders, customerId, limit, offset))

    @app.get("/orders/{orderId}")
    async def get_order(orderId: str) -> JSONResponse:
        order = await asyncio.to_thread(store.get_order, orderId)
        if order is None:
            raise UpstreamFailure(404, "not_found", f"Unknown order {orderId}.")
        return JSONResponse(order)

    @app.post("/orders/{orderId}/cancel")
    async def cancel_order(request: Request, orderId: str, body: CancelBody) -> JSONResponse:
        order = await store.cancel(orderId, body.reason, request.headers.get("X-Correlation-Id", ""))
        return JSONResponse(order)

    @app.post("/orders/{orderId}/ship")
    async def ship_order(orderId: str) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(store.ship, orderId))

    return app
