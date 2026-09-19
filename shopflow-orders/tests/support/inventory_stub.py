"""A controllable ``shopflow-inventory`` stub for fault injection.

The integration suite normally talks to the **real** inventory service
(``INVENTORY_BASE_URL``, contracts.md §4). For the failures that cannot be
provoked through a healthy service - an unreachable inventory, a lost reservation
race, an ambiguous reserve (the reservation happened but the answer never arrived)
or a failing release - this stub implements the same contract on a real socket and
can be switched into a fault mode.

The stub is deliberately strict: it implements the documented all-or-nothing
reservation, the ``unique(order_id, operation)`` idempotency of movements and the
bounded restoration of a release, so a retry that would double-decrement or
double-restore is visible as a movement/stock assertion rather than being hidden.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: contracts.md §10 seeded catalogue.
DEFAULT_PRODUCTS: dict[str, dict[str, Any]] = {
    "p-1001": {"sku": "SF-TEE-001", "name": "ShopFlow Classic Tee", "price": Decimal("19.99")},
    "p-1002": {"sku": "SF-MUG-002", "name": "ShopFlow Ceramic Mug", "price": Decimal("12.50")},
    "p-1003": {"sku": "SF-HOOD-003", "name": "ShopFlow Hoodie", "price": Decimal("49.00")},
    "p-1004": {"sku": "SF-STICK-004", "name": "ShopFlow Sticker Pack", "price": Decimal("4.25")},
    "p-1005": {"sku": "SF-CAP-005", "name": "ShopFlow Cap", "price": Decimal("22.00")},
}

DEFAULT_STOCK: dict[str, int] = {"p-1001": 50, "p-1002": 25, "p-1003": 10, "p-1004": 100, "p-1005": 0}

#: fault modes of the stub.
MODE_NORMAL = "normal"
#: answer ``503`` to ``POST /inventory/availability``.
MODE_AVAILABILITY_DOWN = "availability_down"
#: answer ``409 insufficient_stock`` to the next reservation (the race was lost).
MODE_RESERVE_RACE_LOST = "reserve_race_lost"
#: answer ``503`` to the next reservation *before* it is recorded.
MODE_RESERVE_DOWN = "reserve_down"
#: record the reservation, then answer ``503``: the caller cannot tell whether it
#: happened (the ambiguous failure of the handoff's crash window).
MODE_RESERVE_AMBIGUOUS = "reserve_ambiguous"
#: answer ``503`` to ``POST /inventory/releases`` (the release stays pending).
MODE_RELEASE_DOWN = "release_down"


class InventoryStubState:
    """Mutable state and fault switches of the stub."""

    def __init__(
        self,
        *,
        products: dict[str, dict[str, Any]] | None = None,
        stock: dict[str, int] | None = None,
    ) -> None:
        self.products = {key: dict(value) for key, value in (products or DEFAULT_PRODUCTS).items()}
        self.stock = dict(stock or DEFAULT_STOCK)
        self.movements: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.mode: str = MODE_NORMAL

    # -- control ------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def reset_calls(self) -> None:
        self.calls = []

    # -- inspection ---------------------------------------------------------
    def movement(self, order_id: str, operation: str) -> dict[str, Any] | None:
        return self.movements.get((order_id, operation))

    def movements_for(self, order_id: str) -> list[dict[str, Any]]:
        """The movements of one order, in the chronological order they were recorded."""

        return [
            {"operation": operation, **value}
            for (key, operation), value in self.movements.items()
            if key == order_id
        ]

    def calls_of(self, operation: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("operation") == operation]

    def snapshot(self) -> dict[str, Any]:
        return {
            "stock": dict(self.stock),
            "movements": [
                {"orderId": order_id, "operation": operation, **value}
                for (order_id, operation), value in sorted(self.movements.items())
            ],
            "calls": list(self.calls),
            "mode": self.mode,
        }


def create_inventory_stub(state: InventoryStubState) -> FastAPI:
    """Build the stub application around ``state``."""

    app = FastAPI(title="inventory-stub", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.stub = state

    def error(code: str, message: str, *, details: dict[str, Any] | None = None) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message, "correlationId": "", "details": details or {}}},
            status_code={
                "validation_error": 400,
                "not_found": 404,
                "unknown_product": 404,
                "insufficient_stock": 409,
                "idempotency_conflict": 409,
                "inventory_unavailable": 503,
            }.get(code, 500),
        )

    def product_view(product_id: str) -> dict[str, Any]:
        product = state.products[product_id]
        quantity = int(state.stock.get(product_id, 0))
        return {
            "productId": product_id,
            "sku": product["sku"],
            "name": product["name"],
            "description": "",
            "price": float(product["price"]),
            "currency": "USD",
            "availableQuantity": quantity,
            "inStock": quantity > 0,
        }

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"status": "ready", "upstreams": {"store": True}})

    @app.get("/products/{productId}")
    async def get_product(productId: str) -> JSONResponse:
        state.calls.append({"operation": "get_product", "productId": productId})
        if productId not in state.products:
            return error("not_found", f"Unknown product {productId}.")
        return JSONResponse(product_view(productId))

    @app.post("/inventory/availability")
    async def availability(request: Request) -> JSONResponse:
        body = await request.json()
        lines = body.get("lines") or []
        state.calls.append({"operation": "availability", "lines": lines})
        if state.mode == MODE_AVAILABILITY_DOWN:
            return error("inventory_unavailable", "The inventory service is unavailable.")
        items = []
        for line in lines:
            product_id = str(line.get("productId"))
            quantity = int(line.get("quantity", 0))
            if product_id not in state.products:
                return error("unknown_product", f"Unknown product {product_id}.", details={"productId": product_id})
            available = max(int(state.stock.get(product_id, 0)), 0)
            items.append(
                {
                    "productId": product_id,
                    "requested": quantity,
                    "available": available,
                    "sufficient": available >= quantity,
                }
            )
        return JSONResponse({"available": all(item["sufficient"] for item in items), "items": items})

    @app.post("/inventory/reservations")
    async def reservations(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        body = await request.json()
        order_id = str(body.get("orderId"))
        lines = body.get("lines") or []
        state.calls.append(
            {
                "operation": "reserve",
                "orderId": order_id,
                "lines": lines,
                "idempotencyKey": idempotency_key,
            }
        )
        expected_key = f"order:{order_id}:reserve"
        if idempotency_key != expected_key:
            return error("validation_error", f"Header Idempotency-Key must be '{expected_key}'.")
        if state.mode == MODE_RESERVE_DOWN:
            return error("inventory_unavailable", "The inventory service is unavailable.")
        if state.mode == MODE_RESERVE_RACE_LOST:
            return error(
                "insufficient_stock",
                "Requested quantity exceeds available stock.",
                details={"orderId": order_id},
            )

        existing = state.movement(order_id, "reserve")
        if existing is not None:
            return JSONResponse({"orderId": order_id, "reserved": True, "replayed": True})

        for line in lines:
            product_id = str(line.get("productId"))
            quantity = int(line.get("quantity", 0))
            if product_id not in state.products:
                return error("unknown_product", f"Unknown product {product_id}.", details={"productId": product_id})
            if int(state.stock.get(product_id, 0)) < quantity:
                return error(
                    "insufficient_stock",
                    "Requested quantity exceeds available stock.",
                    details={
                        "productId": product_id,
                        "requested": quantity,
                        "available": int(state.stock.get(product_id, 0)),
                    },
                )
        for line in lines:
            product_id = str(line.get("productId"))
            state.stock[product_id] = int(state.stock.get(product_id, 0)) - int(line.get("quantity", 0))
        state.movements[(order_id, "reserve")] = {"lines": lines, "payload": {"lines": lines}}

        if state.mode == MODE_RESERVE_AMBIGUOUS:
            return error("inventory_unavailable", "The reservation was recorded but the answer was lost.")
        return JSONResponse({"orderId": order_id, "reserved": True, "replayed": False})

    @app.post("/inventory/releases")
    async def releases(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        body = await request.json()
        order_id = str(body.get("orderId"))
        lines = body.get("lines") or []
        state.calls.append(
            {
                "operation": "release",
                "orderId": order_id,
                "lines": lines,
                "idempotencyKey": idempotency_key,
            }
        )
        expected_key = f"order:{order_id}:release"
        if idempotency_key != expected_key:
            return error("validation_error", f"Header Idempotency-Key must be '{expected_key}'.")
        if state.mode == MODE_RELEASE_DOWN:
            return error("inventory_unavailable", "The inventory service is unavailable.")

        if state.movement(order_id, "release") is not None:
            return JSONResponse({"orderId": order_id, "restored": False, "replayed": True})
        reservation = state.movement(order_id, "reserve")
        if reservation is None:
            # contracts.md §4: a release for an order that was never reserved is a
            # safe no-op, so retries after an ambiguous failure never fail.
            return JSONResponse({"orderId": order_id, "restored": False, "replayed": False})
        for line in reservation["lines"]:
            product_id = str(line.get("productId"))
            state.stock[product_id] = int(state.stock.get(product_id, 0)) + int(line.get("quantity", 0))
        state.movements[(order_id, "release")] = {"lines": reservation["lines"], "payload": {"lines": lines}}
        return JSONResponse({"orderId": order_id, "restored": True, "replayed": False})

    @app.get("/_stub/state")
    async def stub_state() -> JSONResponse:
        return JSONResponse(state.snapshot())

    @app.post("/_stub/mode")
    async def stub_mode(request: Request) -> JSONResponse:
        body = await request.json()
        state.set_mode(str(body.get("mode", MODE_NORMAL)))
        return JSONResponse({"mode": state.mode})

    return app


__all__ = [
    "DEFAULT_PRODUCTS",
    "DEFAULT_STOCK",
    "InventoryStubState",
    "MODE_AVAILABILITY_DOWN",
    "MODE_NORMAL",
    "MODE_RELEASE_DOWN",
    "MODE_RESERVE_AMBIGUOUS",
    "MODE_RESERVE_DOWN",
    "MODE_RESERVE_RACE_LOST",
    "create_inventory_stub",
]
