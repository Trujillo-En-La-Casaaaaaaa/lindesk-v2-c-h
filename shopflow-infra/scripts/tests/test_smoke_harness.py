"""Self-tests for the end-to-end smoke harness (``scripts/smoke_test.py``).

A completion proof that passes vacuously is worse than no proof at all, so these
tests verify the *harness*, not the ecosystem:

* against a contract-faithful fake of the public gateway and the provider
  emulator (contracts.md §2/§3/§7) all nine scenarios pass, ``main`` returns
  ``0``, and exactly one line per scenario is printed;
* for every failure mode the handoff requires the smoke test to catch — a wrong
  order status, no stock change, a duplicated notification, a missing
  ``cancelledAt``, a cancellable ``SHIPPED`` order, an order created for
  ``p-1005``, a missing ``correlationId`` — the corresponding scenario fails and
  ``main`` returns ``1``.

Only a live stack can show that the real services behave; that is what
``python scripts/smoke_test.py`` does against the running compose stack (README
§7). Here the transport is replaced, so nothing leaves the process.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import seed
import shopflow_http as http
import smoke_test

GATEWAY = "http://gateway.test"
PROVIDER = "http://provider.test"
TIMESTAMP = "2026-09-18T12:00:00Z"

DOCUMENTED = smoke_test.DOCUMENTED_PRODUCTS
SEEDED_STOCK = {product.product_id: product.seeded_stock for product in seed.SEEDED_PRODUCTS}


def error_envelope(
    code: str,
    *,
    correlation_id: str | None = "corr-fake",
    message: str = "fake failure",
) -> dict[str, Any]:
    envelope: dict[str, Any] = {"code": code, "message": message, "details": {}}
    if correlation_id is not None:
        envelope["correlationId"] = correlation_id
    return {"error": envelope}


def response(status_code: int, payload: Any, url: str = "") -> http.Response:
    return http.Response(
        status_code=status_code,
        text=json.dumps(payload),
        headers={"content-type": "application/json"},
        url=url,
    )


class FakeEcosystem:
    """A deliberately small, contract-faithful stand-in for gateway + emulator.

    ``faults`` injects one named violation at a time so the harness can be proven
    to catch it.
    """

    def __init__(self, faults: set[str] | None = None) -> None:
        self.faults = set(faults or ())
        self.stock = dict(SEEDED_STOCK)
        self.orders: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []

    # -- transport ---------------------------------------------------------- #
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> http.Response:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if url.startswith(PROVIDER):
            return self._provider(method, parsed.path, query, url)
        return self._gateway(method, parsed.path, query, json_body or {}, headers or {}, url)

    # -- provider emulator (contracts.md §7) -------------------------------- #
    def _provider(self, method: str, path: str, query: dict[str, list[str]], url: str) -> http.Response:
        if method == "GET" and path == "/messages":
            order_id = (query.get("orderId") or [None])[0]
            items = [m for m in self.messages if order_id is None or m["orderId"] == order_id]
            return response(200, {"items": items, "total": len(items)}, url)
        if method == "GET" and path == "/health":
            return response(200, {"status": "ok"}, url)
        return response(404, error_envelope("not_found"), url)

    def notify(self, order: dict[str, Any], template: str) -> None:
        """Deliver one message per (orderId, template), like the real consumer."""

        if any(m["orderId"] == order["orderId"] and m["template"] == template for m in self.messages):
            return
        self.messages.append(
            {
                "messageId": smoke_test.deterministic_message_id(template, order["orderId"]),
                "orderId": order["orderId"],
                "template": template,
                "to": "customer@shopflow.test",
                "receivedAt": TIMESTAMP,
            }
        )
        if template == "order_confirmation" and "duplicate_confirmation_message" in self.faults:
            self.messages.append(dict(self.messages[-1]))

    # -- public gateway (contracts.md §2/§3) -------------------------------- #
    def _product(self, product_id: str) -> dict[str, Any]:
        documented = next(p for p in DOCUMENTED if p.product_id == product_id)
        available = self.stock[product_id]
        return {
            "productId": product_id,
            "sku": documented.sku,
            "name": documented.name,
            "description": "",
            "price": float(documented.price),
            "currency": "USD",
            "availableQuantity": available,
            "inStock": available > 0,
        }

    def _order_view(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "orderId": order["orderId"],
            "status": order["status"],
            "customerId": order["customerId"],
            "createdAt": order["createdAt"],
            "updatedAt": order["updatedAt"],
            "confirmedAt": order["confirmedAt"],
            "shippedAt": order["shippedAt"],
            "cancelledAt": order["cancelledAt"],
            "cancellationReason": order["cancellationReason"],
            "total": order["total"],
            "lines": order["lines"],
        }

    def _gateway(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
        headers: dict[str, str],
        url: str,
    ) -> http.Response:
        if method == "GET" and path == "/api/products":
            items = [self._product(product_id) for product_id in self.stock]
            if "catalog_four_products" in self.faults:
                items = items[:-1]
            return response(200, {"items": items, "total": len(items)}, url)

        match = re.fullmatch(r"/api/products/([\w-]+)", path)
        if method == "GET" and match:
            product_id = match.group(1)
            if product_id not in self.stock:
                return response(404, error_envelope("not_found"), url)
            return response(200, self._product(product_id), url)

        if method == "POST" and path == "/api/orders":
            return self._create_order(body, url)

        if method == "GET" and path == "/api/orders":
            customer_id = (query.get("customerId") or [""])[0]
            items = [self._order_view(o) for o in self.orders.values() if o["customerId"] == customer_id]
            return response(200, {"items": items, "total": len(items)}, url)

        match = re.fullmatch(r"/api/orders/([\w-]+)", path)
        if method == "GET" and match:
            order = self.orders.get(match.group(1))
            if order is None:
                return response(404, error_envelope("not_found"), url)
            return response(200, self._order_view(order), url)

        match = re.fullmatch(r"/api/orders/([\w-]+)/cancel", path)
        if method == "POST" and match:
            return self._cancel(match.group(1), body, url)

        match = re.fullmatch(r"/api/orders/([\w-]+)/ship", path)
        if method == "POST" and match:
            return self._ship(match.group(1), headers, url)

        return response(404, error_envelope("not_found"), url)

    def _create_order(self, body: dict[str, Any], url: str) -> http.Response:
        lines = body.get("lines") or []
        if "zero_stock_order_created" not in self.faults:
            for line in lines:
                product_id, quantity = line["productId"], int(line["quantity"])
                if product_id not in self.stock:
                    return response(404, error_envelope("unknown_product"), url)
                if self.stock[product_id] < quantity:
                    return response(
                        409,
                        error_envelope(
                            "insufficient_stock",
                            correlation_id=None if "missing_correlation_id" in self.faults else "corr-fake",
                        ),
                        url,
                    )

        order_id = str(uuid.uuid4())
        order_lines = []
        total = Decimal("0")
        for line in lines:
            product = self._product(line["productId"])
            quantity = int(line["quantity"])
            line_total = round(product["price"] * quantity, 2)
            total += Decimal(str(line_total))
            order_lines.append(
                {
                    "productId": product["productId"],
                    "sku": product["sku"],
                    "name": product["name"],
                    "quantity": quantity,
                    "unitPrice": product["price"],
                    "lineTotal": line_total,
                }
            )

        order: dict[str, Any] = {
            "orderId": order_id,
            "customerId": body.get("customerId"),
            "status": "CONFIRMED",
            "createdAt": TIMESTAMP,
            "updatedAt": TIMESTAMP,
            "confirmedAt": TIMESTAMP,
            "shippedAt": None,
            "cancelledAt": None,
            "cancellationReason": None,
            "total": float(total),
            "lines": order_lines,
        }
        self.orders[order_id] = order

        if "no_stock_decrement" not in self.faults:
            for line in order_lines:
                self.stock[line["productId"]] -= line["quantity"]

        self.notify(order, "order_confirmation")

        if "order_status_created" in self.faults:
            order["status"] = "CREATED"
            order["confirmedAt"] = None

        return response(201, self._order_view(order), url)

    def _cancel(self, order_id: str, body: dict[str, Any], url: str) -> http.Response:
        order = self.orders.get(order_id)
        if order is None:
            return response(404, error_envelope("not_found"), url)

        reason = str(body.get("reason") or "").strip()
        if not reason or len(reason) > 200:
            return response(400, error_envelope("invalid_reason"), url)

        if order["status"] == "SHIPPED" and "shipped_cancel_allowed" not in self.faults:
            return response(409, error_envelope("order_shipped"), url)

        if order["status"] == "CANCELLED":
            if "duplicate_cancellation_changes_stock" in self.faults:
                order["cancelledAt"] = "2026-09-18T13:00:00Z"
                for line in order["lines"]:
                    self.stock[line["productId"]] += line["quantity"]
            return response(200, self._order_view(order), url)

        order["status"] = "CANCELLED"
        order["cancelledAt"] = None if "cancel_without_cancelled_at" in self.faults else TIMESTAMP
        order["cancellationReason"] = reason
        if "no_stock_restore" not in self.faults:
            for line in order["lines"]:
                self.stock[line["productId"]] += line["quantity"]
        self.notify(order, "order_cancellation")
        return response(200, self._order_view(order), url)

    def _ship(self, order_id: str, headers: dict[str, str], url: str) -> http.Response:
        if (headers.get("X-ShopFlow-Role") or "").strip() != "admin":
            return response(403, error_envelope("forbidden"), url)
        order = self.orders.get(order_id)
        if order is None:
            return response(404, error_envelope("not_found"), url)
        if order["status"] != "CONFIRMED":
            return response(409, error_envelope("invalid_status_transition"), url)
        order["status"] = "SHIPPED"
        order["shippedAt"] = TIMESTAMP
        return response(200, self._order_view(order), url)


ARGS = [
    "--gateway-url",
    GATEWAY,
    "--provider-url",
    PROVIDER,
    "--settle-seconds",
    "0",
    "--event-timeout",
    "5",
    "--request-timeout",
    "5",
]


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch):
    """Install a fake transport and return a factory for ecosystems with faults."""

    def install(*faults: str) -> FakeEcosystem:
        ecosystem = FakeEcosystem(set(faults))
        monkeypatch.setattr(http, "request", ecosystem.request)
        return ecosystem

    return install


def test_the_nine_scenarios_pass_against_a_contract_faithful_fake(fake, capsys: pytest.CaptureFixture[str]) -> None:
    ecosystem = fake()

    assert smoke_test.main(ARGS) == 0

    out = capsys.readouterr().out
    printed = [line for line in out.splitlines() if line.startswith(("PASS", "FAIL"))]
    assert len(printed) == 9, out
    assert [line.split()[0] for line in printed] == ["PASS"] * 9
    assert [line.split()[1] for line in printed] == [str(n) for n in range(1, 10)]
    assert "smoke test: PASSED (9 scenarios" in out

    # the fake really did exercise the ecosystem: two orders created (one shipped),
    # and the notifications trail is exactly confirmation -> cancellation -> confirmation
    assert len(ecosystem.orders) == 2
    assert {order["status"] for order in ecosystem.orders.values()} == {"CANCELLED", "SHIPPED"}
    assert [message["template"] for message in ecosystem.messages] == [
        "order_confirmation",
        "order_cancellation",
        "order_confirmation",
    ]


@pytest.mark.parametrize(
    ("fault", "scenario"),
    [
        ("catalog_four_products", 1),
        ("order_status_created", 2),
        ("no_stock_decrement", 3),
        ("duplicate_confirmation_message", 4),
        ("cancel_without_cancelled_at", 5),
        ("no_stock_restore", 6),
        ("duplicate_cancellation_changes_stock", 7),
        ("shipped_cancel_allowed", 8),
        ("zero_stock_order_created", 9),
        ("missing_correlation_id", 9),
    ],
)
def test_every_required_failure_mode_fails_closed(
    fake,
    capsys: pytest.CaptureFixture[str],
    fault: str,
    scenario: int,
) -> None:
    fake(fault)

    assert smoke_test.main(ARGS) == 1

    out = capsys.readouterr().out
    assert f"FAIL  {scenario}  " in out, out
    assert "smoke test: FAILED" in out
    assert f"PASS  {scenario}  " not in out, "the failing scenario must not also be reported as passing"


def test_an_unreachable_gateway_is_reported_as_a_failure(fake, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> http.Response:
        raise http.TransportError("connection refused (test)")

    monkeypatch.setattr(http, "request", boom)

    assert smoke_test.main(ARGS) == 1
    assert "FAIL  1  " in capsys.readouterr().out


def test_stock_deltas_are_measured_not_assumed(fake, capsys: pytest.CaptureFixture[str]) -> None:
    """A tenant that already consumed stock must not break scenarios 3 and 6."""

    ecosystem = fake()
    ecosystem.stock["p-1001"] = 7
    ecosystem.stock["p-1004"] = 2

    assert smoke_test.main(ARGS) == 0
    out = capsys.readouterr().out
    assert "p-1001 availableQuantity 7 -> 4 (-3)" in out
    assert "p-1001 availableQuantity restored to 7" in out
