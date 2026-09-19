#!/usr/bin/env python
"""End-to-end black-box smoke test for the ShopFlow ecosystem (HANDOFF #8).

The smoke test is the ecosystem's completion proof. It drives the **public
gateway** (``GATEWAY_BASE_URL``, default ``http://localhost:8080``) for every
business action and reads the provider emulator's deterministic test affordance
(``GET /messages``, ``SHOPFLOW_PROVIDER_URL``, default ``http://localhost:8081``)
to count notifications. It never touches a database and never calls a backend
service directly.

Scenarios (one printed line each, all values observed, never assumed):

1. the catalog is served through the gateway (five seeded products)
2. creating an in-stock order returns ``201`` and ``status=CONFIRMED``
3. stock decreased by exactly the ordered quantity
4. the emulator holds exactly one deterministic ``order_confirmation`` message
5. cancelling with a reason returns ``200`` with ``cancelledAt``/``cancellationReason``
6. stock is restored to its pre-order value
7. repeating the cancellation changes no stock and keeps a single ``cancelledAt``
8. a ``SHIPPED`` order cannot be cancelled (``409 order_shipped``)
9. ``p-1005`` (zero stock) is rejected with ``409 insufficient_stock`` and creates no order

Exit code is ``0`` only when every assertion holds; any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shopflow_http as http  # noqa: E402  (path bootstrap above)

#: The gateway base url is the one named by contracts.md §2 for local use.
GATEWAY_BASE_URL_ENV = "GATEWAY_BASE_URL"
#: Host-side affordance: from a workstation the emulator is reached on its
#: published port, while ``PROVIDER_BASE_URL`` (contracts.md §11) is the
#: in-network name used by shopflow-notifications. Keeping the two apart means
#: sourcing ``.env`` can never point the smoke test at an unresolvable hostname.
PROVIDER_URL_ENV = "SHOPFLOW_PROVIDER_URL"

DEFAULT_GATEWAY_BASE_URL = "http://localhost:8080"
DEFAULT_PROVIDER_BASE_URL = f"http://localhost:{os.environ.get('PROVIDER_PORT', '8081')}"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_EVENT_TIMEOUT_SECONDS = 60.0
DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class DocumentedProduct:
    """One row of the deterministic seed table (contracts.md §10)."""

    product_id: str
    sku: str
    name: str
    price: Decimal


DOCUMENTED_PRODUCTS: tuple[DocumentedProduct, ...] = (
    DocumentedProduct("p-1001", "SF-TEE-001", "ShopFlow Classic Tee", Decimal("19.99")),
    DocumentedProduct("p-1002", "SF-MUG-002", "ShopFlow Ceramic Mug", Decimal("12.50")),
    DocumentedProduct("p-1003", "SF-HOOD-003", "ShopFlow Hoodie", Decimal("49.00")),
    DocumentedProduct("p-1004", "SF-STICK-004", "ShopFlow Sticker Pack", Decimal("4.25")),
    DocumentedProduct("p-1005", "SF-CAP-005", "ShopFlow Cap", Decimal("22.00")),
)


class SmokeFailure(Exception):
    """An asserted user-visible behaviour did not hold."""


def require(condition: bool, message: str) -> None:
    """Raise :class:`SmokeFailure` when ``condition`` is falsy."""

    if not condition:
        raise SmokeFailure(message)


def deterministic_message_id(template: str, order_id: str) -> str:
    """contracts.md §7: ``"msg-" + sha256(template + ":" + orderId).hexdigest()[:16]``."""

    return "msg-" + hashlib.sha256(f"{template}:{order_id}".encode("utf-8")).hexdigest()[:16]


class SmokeTest:
    """Stateful black-box scenario runner."""

    def __init__(
        self,
        *,
        gateway_base_url: str,
        provider_base_url: str,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        event_timeout: float = DEFAULT_EVENT_TIMEOUT_SECONDS,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gateway = gateway_base_url.rstrip("/")
        self.provider = provider_base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.event_timeout = event_timeout
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.clock = clock

        self.customer_id = f"smoke-order-{uuid.uuid4()}"
        self.stock_product = "p-1001"
        self.ship_product = "p-1004"
        self.zero_stock_product = "p-1005"
        self.order_quantity = 3

        self.order_id: str | None = None
        self.stock_before: int | None = None
        self.cancelled_at: str | None = None
        self.reason = "customer changed their mind"

    # ------------------------------------------------------------------ #
    # HTTP plumbing (gateway for business actions, emulator for counting)
    # ------------------------------------------------------------------ #
    def api(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> http.Response:
        url = http.with_query(f"{self.gateway}{path}", params)
        return http.request(method, url, json_body=json_body, headers=headers, timeout=self.request_timeout)

    def provider_messages(self, order_id: str) -> list[dict[str, Any]]:
        response = http.get(f"{self.provider}/messages", params={"orderId": order_id}, timeout=self.request_timeout)
        require(response.ok, f"GET {self.provider}/messages failed: {response.describe()}")
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        require(isinstance(items, list), f"unexpected emulator payload: {payload!r}")
        return items

    def message_count(self, order_id: str, template: str) -> int:
        return len([item for item in self.provider_messages(order_id) if item.get("template") == template])

    def wait_for_messages(self, order_id: str, template: str, *, minimum: int = 1) -> list[dict[str, Any]]:
        """Wait until at least ``minimum`` messages exist, settle, then recount.

        The settle window gives a wrongly duplicated delivery the chance to show
        up before the caller asserts an exact count.
        """

        deadline = self.clock() + self.event_timeout
        while True:
            messages = [item for item in self.provider_messages(order_id) if item.get("template") == template]
            if len(messages) >= minimum:
                break
            if self.clock() >= deadline:
                raise SmokeFailure(
                    f"timed out after {self.event_timeout:g}s waiting for a '{template}' message for order {order_id} "
                    f"(saw {len(messages)})"
                )
            self.sleep(self.poll_seconds)

        self.sleep(self.settle_seconds)
        return [item for item in self.provider_messages(order_id) if item.get("template") == template]

    def product(self, product_id: str) -> dict[str, Any]:
        response = self.api("GET", f"/api/products/{product_id}")
        require(response.status_code == 200, f"GET /api/products/{product_id} -> {response.describe()}")
        payload = response.json()
        require(isinstance(payload, dict), f"unexpected product payload for {product_id}: {payload!r}")
        return payload

    def available(self, product_id: str) -> int:
        try:
            return int(self.product(product_id)["availableQuantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeFailure(f"{product_id} did not report a numeric availableQuantity: {exc}") from exc

    def wait_for_stock(self, product_id: str, expected: int) -> int:
        """Wait for the stock level to reach ``expected`` (the release is asynchronous)."""

        deadline = self.clock() + self.event_timeout
        observed = self.available(product_id)
        while observed != expected:
            if self.clock() >= deadline:
                raise SmokeFailure(
                    f"{product_id} availableQuantity stayed at {observed}, expected {expected} within {self.event_timeout:g}s"
                )
            self.sleep(self.poll_seconds)
            observed = self.available(product_id)
        return observed

    # ------------------------------------------------------------------ #
    # Scenarios
    # ------------------------------------------------------------------ #
    def list_products(self) -> str:
        response = self.api("GET", "/api/products")
        require(response.ok, f"GET /api/products -> {response.describe()}")
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        require(isinstance(items, list), f"GET /api/products returned no item list: {payload!r}")

        expected_ids = sorted(product.product_id for product in DOCUMENTED_PRODUCTS)
        served_ids = sorted(str(item.get("productId")) for item in items)
        require(
            len(items) == len(DOCUMENTED_PRODUCTS),
            f"expected {len(DOCUMENTED_PRODUCTS)} products from GET /api/products, got {len(items)}",
        )
        require(served_ids == expected_ids, f"catalog ids {served_ids} != documented {expected_ids}")

        by_id = {str(item["productId"]): item for item in items}
        for product in DOCUMENTED_PRODUCTS:
            require(str(by_id[product.product_id].get("sku")) == product.sku,
                    f"{product.product_id}: sku {by_id[product.product_id].get('sku')!r} != {product.sku!r}")
            require(Decimal(str(by_id[product.product_id].get("price"))) == product.price,
                    f"{product.product_id}: price {by_id[product.product_id].get('price')!r} != {product.price}")

        return f"GET /api/products -> 200 with the 5 seeded products ({', '.join(served_ids)})"

    def create_order(self) -> str:
        self.stock_before = self.available(self.stock_product)
        require(
            self.stock_before >= self.order_quantity,
            f"{self.stock_product} has only {self.stock_before} available; the smoke test needs {self.order_quantity}",
        )

        body = {
            "customerId": self.customer_id,
            "lines": [{"productId": self.stock_product, "quantity": self.order_quantity}],
        }
        response = self.api("POST", "/api/orders", json_body=body)
        require(response.status_code == 201, f"POST /api/orders -> {response.describe()}, expected 201")

        order = response.json()
        require(str(order.get("status")) == "CONFIRMED", f"expected status=CONFIRMED, got {order.get('status')!r}")
        require(bool(order.get("confirmedAt")), "expected a non-null confirmedAt on a CONFIRMED order")
        lines = order.get("lines")
        require(isinstance(lines, list) and lines, f"expected the order lines in the order view, got {lines!r}")
        require(lines[0].get("productId") == self.stock_product,
                f"expected the order line for {self.stock_product}, got {lines[0].get('productId')!r}")
        require(int(lines[0].get("quantity", 0)) == self.order_quantity,
                f"expected quantity {self.order_quantity}, got {lines[0].get('quantity')!r}")

        self.order_id = str(order["orderId"])
        return f"POST /api/orders -> 201 status=CONFIRMED orderId={self.order_id} total={order.get('total')}"

    def stock_decremented(self) -> str:
        assert self.order_id and self.stock_before is not None  # guarded by scenario order
        expected = self.stock_before - self.order_quantity
        current = self.available(self.stock_product)
        require(
            current == expected,
            f"{self.stock_product} availableQuantity is {current}, expected {expected} "
            f"after ordering {self.order_quantity} of {self.stock_before}",
        )
        return f"{self.stock_product} availableQuantity {self.stock_before} -> {current} (-{self.order_quantity})"

    def confirmation_message(self) -> str:
        assert self.order_id  # guarded by scenario order
        messages = self.wait_for_messages(self.order_id, "order_confirmation", minimum=1)
        require(len(messages) == 1, f"expected exactly 1 order_confirmation message, found {len(messages)}")
        expected_id = deterministic_message_id("order_confirmation", self.order_id)
        require(messages[0].get("messageId") == expected_id,
                f"order_confirmation messageId {messages[0].get('messageId')!r} != deterministic {expected_id!r}")
        require(messages[0].get("orderId") == self.order_id, "the message is recorded against the wrong orderId")
        return f"emulator holds exactly 1 order_confirmation for {self.order_id} (messageId={expected_id})"

    def cancel_order(self) -> str:
        assert self.order_id  # guarded by scenario order
        response = self.api("POST", f"/api/orders/{self.order_id}/cancel", json_body={"reason": self.reason})
        require(response.status_code == 200, f"POST /api/orders/{{id}}/cancel -> {response.describe()}, expected 200")

        order = response.json()
        require(str(order.get("status")) == "CANCELLED", f"expected status=CANCELLED, got {order.get('status')!r}")
        cancelled_at = order.get("cancelledAt")
        require(isinstance(cancelled_at, str) and cancelled_at, f"expected a non-null cancelledAt, got {cancelled_at!r}")
        require(order.get("cancellationReason") == self.reason,
                f"cancellationReason {order.get('cancellationReason')!r} != {self.reason!r}")
        self.cancelled_at = cancelled_at
        return f"POST /api/orders/{{id}}/cancel -> 200 status=CANCELLED cancelledAt={cancelled_at}"

    def stock_restored(self) -> str:
        assert self.stock_before is not None  # guarded by scenario order
        current = self.wait_for_stock(self.stock_product, self.stock_before)
        return f"{self.stock_product} availableQuantity restored to {current} (was {self.stock_before} before the order)"

    def duplicate_cancellation(self) -> str:
        assert self.order_id and self.cancelled_at and self.stock_before is not None  # guarded by scenario order
        response = self.api("POST", f"/api/orders/{self.order_id}/cancel", json_body={"reason": self.reason})
        require(
            response.status_code in (200, 409),
            f"repeating the cancellation -> {response.describe()}, expected 200 (safe repeat) or 409 order_not_cancellable",
        )
        if response.status_code == 409:
            require(response.error_code() == "order_not_cancellable",
                    f"repeating the cancellation -> 409 {response.error_code()!r}, expected order_not_cancellable")

        reread = self.api("GET", f"/api/orders/{self.order_id}")
        require(reread.status_code == 200, f"GET /api/orders/{{id}} -> {reread.describe()}")
        order = reread.json()
        require(str(order.get("status")) == "CANCELLED", f"order status became {order.get('status')!r} after a repeat cancel")
        require(order.get("cancelledAt") == self.cancelled_at,
                f"cancelledAt changed on the repeat cancellation: {self.cancelled_at!r} -> {order.get('cancelledAt')!r}")
        require(order.get("cancellationReason") == self.reason, "cancellationReason changed on the repeat cancellation")

        current = self.available(self.stock_product)
        require(current == self.stock_before,
                f"{self.stock_product} changed to {current} after the repeat cancellation (expected {self.stock_before})")

        messages = self.wait_for_messages(self.order_id, "order_cancellation", minimum=1)
        require(len(messages) == 1, f"expected exactly 1 order_cancellation message, found {len(messages)}")

        return (
            f"repeat cancel -> {response.status_code}: single cancelledAt={self.cancelled_at}, "
            f"{self.stock_product} stayed {current}, still exactly 1 order_cancellation message"
        )

    def shipped_order_cannot_be_cancelled(self) -> str:
        available = self.available(self.ship_product)
        require(available >= 1, f"{self.ship_product} has no stock ({available}); cannot create the shipping scenario")

        created = self.api(
            "POST",
            "/api/orders",
            json_body={"customerId": self.customer_id, "lines": [{"productId": self.ship_product, "quantity": 1}]},
        )
        require(created.status_code == 201, f"POST /api/orders -> {created.describe()}, expected 201")
        order = created.json()
        require(str(order.get("status")) == "CONFIRMED", f"expected a CONFIRMED order, got {order.get('status')!r}")
        order_id = str(order["orderId"])

        shipped = self.api("POST", f"/api/orders/{order_id}/ship", headers={"X-ShopFlow-Role": "admin"})
        require(shipped.status_code == 200, f"POST /api/orders/{{id}}/ship -> {shipped.describe()}, expected 200")
        shipped_order = shipped.json()
        require(str(shipped_order.get("status")) == "SHIPPED", f"expected status=SHIPPED, got {shipped_order.get('status')!r}")
        require(bool(shipped_order.get("shippedAt")), "expected a non-null shippedAt on a SHIPPED order")

        cancelled = self.api("POST", f"/api/orders/{order_id}/cancel", json_body={"reason": "too late, already shipped"})
        require(cancelled.status_code == 409, f"cancelling a SHIPPED order -> {cancelled.describe()}, expected 409")
        require(cancelled.error_code() == "order_shipped",
                f"cancelling a SHIPPED order -> code {cancelled.error_code()!r}, expected order_shipped")

        reread = self.api("GET", f"/api/orders/{order_id}")
        require(reread.status_code == 200, f"GET /api/orders/{{id}} -> {reread.describe()}")
        require(str(reread.json().get("status")) == "SHIPPED",
                f"the SHIPPED order changed to {reread.json().get('status')!r} after the rejected cancellation")

        return f"ship {order_id} -> SHIPPED; cancel -> 409 order_shipped and the order stayed SHIPPED"

    def insufficient_stock(self) -> str:
        customer_id = f"smoke-zero-stock-{uuid.uuid4()}"
        response = self.api(
            "POST",
            "/api/orders",
            json_body={"customerId": customer_id, "lines": [{"productId": self.zero_stock_product, "quantity": 1}]},
        )
        require(response.status_code == 409,
                f"POST /api/orders for {self.zero_stock_product} -> {response.describe()}, expected 409")
        require(response.error_code() == "insufficient_stock",
                f"expected error code insufficient_stock, got {response.error_code()!r}")
        require(bool(response.correlation_id()), "the error envelope did not carry a correlationId (contracts.md §1)")

        orders = self.api("GET", "/api/orders", params={"customerId": customer_id})
        require(orders.status_code == 200, f"GET /api/orders?customerId= -> {orders.describe()}")
        payload = orders.json()
        total = payload.get("total") if isinstance(payload, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        require(total == 0 and not items,
                f"expected no order for {customer_id}, got total={total!r} items={items!r}")
        require(self.available(self.zero_stock_product) == 0,
                f"{self.zero_stock_product} should still have zero stock")

        return f"order for {self.zero_stock_product} -> 409 insufficient_stock and no order created for {customer_id}"


#: ``(number, name, method)`` — printed one line each, in execution order.
SCENARIOS: tuple[tuple[int, str, str], ...] = (
    (1, "list-products-through-gateway", "list_products"),
    (2, "create-order-confirms", "create_order"),
    (3, "stock-decremented-after-order", "stock_decremented"),
    (4, "exactly-one-confirmation-message", "confirmation_message"),
    (5, "cancel-order-with-reason", "cancel_order"),
    (6, "stock-restored-after-cancellation", "stock_restored"),
    (7, "duplicate-cancellation-is-inert", "duplicate_cancellation"),
    (8, "shipped-order-cannot-be-cancelled", "shipped_order_cannot_be_cancelled"),
    (9, "insufficient-stock-blocks-creation", "insufficient_stock"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Black-box end-to-end smoke test for the local ShopFlow stack (gateway only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--gateway-url", default=os.environ.get(GATEWAY_BASE_URL_ENV, DEFAULT_GATEWAY_BASE_URL),
                        help=f"public gateway base url (env: {GATEWAY_BASE_URL_ENV})")
    parser.add_argument("--provider-url", default=os.environ.get(PROVIDER_URL_ENV, DEFAULT_PROVIDER_BASE_URL),
                        help=f"provider emulator base url (env: {PROVIDER_URL_ENV})")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                        help="per-request timeout in seconds")
    parser.add_argument("--event-timeout", type=float, default=DEFAULT_EVENT_TIMEOUT_SECONDS,
                        help="seconds to wait for asynchronous effects (notifications, stock release)")
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS,
                        help="seconds to wait before asserting an exact notification count")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    test = SmokeTest(
        gateway_base_url=args.gateway_url,
        provider_base_url=args.provider_url,
        request_timeout=args.request_timeout,
        event_timeout=args.event_timeout,
        settle_seconds=args.settle_seconds,
    )

    print(
        f"smoke test: gateway={test.gateway} provider={test.provider} transport={http.backend()} "
        f"event-timeout={args.event_timeout:g}s",
        flush=True,
    )

    for number, name, method_name in SCENARIOS:
        scenario = getattr(test, method_name)
        try:
            detail = scenario()
        except SmokeFailure as exc:
            print(f"FAIL  {number}  {name} -> {exc}")
            print("smoke test: FAILED")
            return 1
        except Exception as exc:  # noqa: BLE001 - any unexpected error is a failure, reported without a traceback
            print(f"FAIL  {number}  {name} -> unexpected {type(exc).__name__}: {exc}")
            print("smoke test: FAILED")
            return 1
        print(f"PASS  {number}  {name} -> {detail}", flush=True)

    print(f"smoke test: PASSED ({len(SCENARIOS)} scenarios, gateway-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
