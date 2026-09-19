"""Contract-faithful ``shopflow-inventory`` HTTP service for the gateway suites.

It implements the inventory API of contracts.md §4 (products, availability,
reservations, releases) with the owned SQL schema and deterministic seed data of
contracts.md §9/§10, backed by SQLite instead of PostgreSQL so the suite needs no
database server. Reservation is all-or-nothing in one transaction and releases
are idempotent, exactly as the contract requires.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from typing import Any

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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price NUMERIC NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stock (
  product_id TEXT PRIMARY KEY REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity >= 0),
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_movements (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('reserve','release')),
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (order_id, operation)
);
"""

#: Deterministic seed data of contracts.md §10 (``p-1005`` has zero stock on purpose).
SEED_PRODUCTS: tuple[tuple[str, str, str, str, float, int], ...] = (
    ("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", "Heavyweight cotton t-shirt.", 19.99, 50),
    ("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", "350 ml ceramic mug.", 12.50, 25),
    ("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", "Brushed fleece hoodie.", 49.00, 10),
    ("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", "Die-cut vinyl stickers.", 4.25, 100),
    ("p-1005", "SF-CAP-005", "ShopFlow Cap", "Six-panel cotton cap.", 22.00, 0),
)


class UpstreamFailure(Exception):
    """An error that must be returned as the contracts.md §1 envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productId: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0)


class AvailabilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[LineIn] = Field(..., min_length=1)


class ReservationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orderId: str = Field(..., min_length=1)
    lines: list[LineIn] = Field(..., min_length=1)


class InventoryStore:
    """SQLite-backed inventory state (own schema: contracts.md §9)."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._seed()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _seed(self) -> None:
        now = iso_now()
        for product_id, sku, name, description, price, quantity in SEED_PRODUCTS:
            self._conn.execute(
                "INSERT OR IGNORE INTO products (id, sku, name, description, price, currency, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'USD', ?, ?)",
                (product_id, sku, name, description, price, now, now),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO stock (product_id, quantity, updated_at) VALUES (?, ?, ?)",
                (product_id, quantity, now),
            )

    def reset(self) -> None:
        """Restore the deterministic seed (used between tests)."""

        with self._lock:
            self._conn.execute("DELETE FROM stock_movements")
            for product_id, _sku, _name, _description, _price, quantity in SEED_PRODUCTS:
                self._conn.execute(
                    "UPDATE stock SET quantity = ?, updated_at = ? WHERE product_id = ?",
                    (quantity, iso_now(), product_id),
                )

    def product_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"])

    def list_products(self, limit: int | None, offset: int | None) -> dict[str, Any]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"])
            rows = self._conn.execute(
                "SELECT id, sku, name, description, price, currency FROM products ORDER BY id"
                " LIMIT ? OFFSET ?",
                (limit if limit is not None else -1, offset or 0),
            ).fetchall()
            items = [self._product_view(row) for row in rows]
        return {"items": items, "total": total}

    def product(self, product_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, sku, name, description, price, currency FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
            return self._product_view(row) if row is not None else None

    def available_quantity(self, product_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT quantity FROM stock WHERE product_id = ?", (product_id,)).fetchone()
            return int(row["quantity"]) if row is not None else None

    def _product_view(self, row: sqlite3.Row) -> dict[str, Any]:
        stock = self._conn.execute("SELECT quantity FROM stock WHERE product_id = ?", (row["id"],)).fetchone()
        available = int(stock["quantity"]) if stock is not None else 0
        return {
            "productId": row["id"],
            "sku": row["sku"],
            "name": row["name"],
            "description": row["description"],
            "price": float(row["price"]),
            "currency": row["currency"],
            "availableQuantity": available,
            "inStock": available > 0,
        }

    def availability(self, lines: list[dict[str, Any]]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        with self._lock:
            for line in lines:
                row = self._conn.execute(
                    "SELECT quantity FROM stock WHERE product_id = ?",
                    (line["productId"],),
                ).fetchone()
                if row is None:
                    raise UpstreamFailure(404, "unknown_product", f"Unknown product {line['productId']}.")
                available = int(row["quantity"])
                items.append(
                    {
                        "productId": line["productId"],
                        "requested": line["quantity"],
                        "available": available,
                        "sufficient": available >= line["quantity"],
                    }
                )
        return {"available": all(item["sufficient"] for item in items), "items": items}

    def reserve(self, order_id: str, lines: list[dict[str, Any]], idempotency_key: str | None) -> dict[str, Any]:
        self._require_key(idempotency_key, order_id, "reserve")
        payload_lines = [{"productId": line["productId"], "quantity": int(line["quantity"])} for line in lines]

        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload FROM stock_movements WHERE order_id = ? AND operation = 'reserve'",
                    (order_id,),
                ).fetchone()
                if existing is not None:
                    previous = json.loads(existing["payload"])
                    conn.execute("ROLLBACK")
                    if previous.get("lines") == payload_lines:
                        return {"orderId": order_id, "reserved": True, "replayed": True}
                    raise UpstreamFailure(409, "idempotency_conflict", "Idempotency key reused with a different body.")

                for line in payload_lines:
                    row = conn.execute(
                        "SELECT quantity FROM stock WHERE product_id = ?",
                        (line["productId"],),
                    ).fetchone()
                    if row is None:
                        conn.execute("ROLLBACK")
                        raise UpstreamFailure(404, "unknown_product", f"Unknown product {line['productId']}.")
                    if int(row["quantity"]) < line["quantity"]:
                        conn.execute("ROLLBACK")
                        raise UpstreamFailure(409, "insufficient_stock", "Requested quantity exceeds available stock.")

                now = iso_now()
                for line in payload_lines:
                    conn.execute(
                        "UPDATE stock SET quantity = quantity - ?, updated_at = ? WHERE product_id = ?",
                        (line["quantity"], now, line["productId"]),
                    )
                conn.execute(
                    "INSERT INTO stock_movements (id, order_id, operation, payload, created_at)"
                    " VALUES (?, ?, 'reserve', ?, ?)",
                    (str(uuid.uuid4()), order_id, json.dumps({"lines": payload_lines}), now),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return {"orderId": order_id, "reserved": True, "replayed": False}

    def release(self, order_id: str, lines: list[dict[str, Any]], idempotency_key: str | None) -> dict[str, Any]:
        self._require_key(idempotency_key, order_id, "release")
        payload_lines = [{"productId": line["productId"], "quantity": int(line["quantity"])} for line in lines]

        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                already_released = conn.execute(
                    "SELECT 1 FROM stock_movements WHERE order_id = ? AND operation = 'release'",
                    (order_id,),
                ).fetchone()
                if already_released is not None:
                    conn.execute("ROLLBACK")
                    return {"orderId": order_id, "restored": False, "replayed": True}

                reservation = conn.execute(
                    "SELECT payload FROM stock_movements WHERE order_id = ? AND operation = 'reserve'",
                    (order_id,),
                ).fetchone()
                if reservation is None:
                    conn.execute("ROLLBACK")
                    return {"orderId": order_id, "restored": False, "replayed": False}

                now = iso_now()
                for line in payload_lines:
                    conn.execute(
                        "UPDATE stock SET quantity = quantity + ?, updated_at = ? WHERE product_id = ?",
                        (line["quantity"], now, line["productId"]),
                    )
                conn.execute(
                    "INSERT INTO stock_movements (id, order_id, operation, payload, created_at)"
                    " VALUES (?, ?, 'release', ?, ?)",
                    (str(uuid.uuid4()), order_id, json.dumps({"lines": payload_lines}), now),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return {"orderId": order_id, "restored": True, "replayed": False}

    @staticmethod
    def _require_key(idempotency_key: str | None, order_id: str, operation: str) -> None:
        expected = f"order:{order_id}:{operation}"
        if idempotency_key != expected:
            raise UpstreamFailure(
                400,
                "validation_error",
                f"Header Idempotency-Key must be {expected}.",
            )


def create_inventory_app(
    db_path: str,
    *,
    log: RequestLog | None = None,
    faults: FaultController | None = None,
) -> FastAPI:
    """Build the harness inventory service (contracts.md §4)."""

    request_log = log if log is not None else RequestLog()
    fault_controller = faults if faults is not None else FaultController()
    store = InventoryStore(db_path)

    app = FastAPI(
        title="shopflow-inventory (contract-faithful test harness)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
        return JSONResponse({"status": "ok", "service": "shopflow-inventory"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"status": "ready", "service": "shopflow-inventory"})

    @app.get("/products")
    async def list_products(limit: int | None = None, offset: int | None = None) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(store.list_products, limit, offset))

    @app.get("/products/{productId}")
    async def get_product(productId: str) -> JSONResponse:
        product = await asyncio.to_thread(store.product, productId)
        if product is None:
            raise UpstreamFailure(404, "not_found", f"Unknown product {productId}.")
        return JSONResponse(product)

    @app.post("/inventory/availability")
    async def availability(body: AvailabilityBody) -> JSONResponse:
        lines = [line.model_dump() for line in body.lines]
        return JSONResponse(await asyncio.to_thread(store.availability, lines))

    @app.post("/inventory/reservations")
    async def reservations(request: Request, body: ReservationBody) -> JSONResponse:
        lines = [line.model_dump() for line in body.lines]
        result = await asyncio.to_thread(
            store.reserve,
            body.orderId,
            lines,
            request.headers.get("Idempotency-Key"),
        )
        return JSONResponse(result)

    @app.post("/inventory/releases")
    async def releases(request: Request, body: ReservationBody) -> JSONResponse:
        lines = [line.model_dump() for line in body.lines]
        result = await asyncio.to_thread(
            store.release,
            body.orderId,
            lines,
            request.headers.get("Idempotency-Key"),
        )
        return JSONResponse(result)

    return app
