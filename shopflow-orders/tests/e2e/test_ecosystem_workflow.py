"""End-to-end tests: the order lifecycle through the public gateway.

Every assertion is made on the public surface: the gateway of contracts.md §2 and the
provider emulator of contracts.md §7. This is the customer-visible behaviour of the
order lifecycle - creation after an inventory check, cancellation with a reason and an
exactly-once stock restoration, shipping, and exactly one notification per event.
"""

from __future__ import annotations

import hashlib
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import run_smoke_test

#: A product with plenty of stock, so the suite can be replayed many times.
PRODUCT = "p-1004"
#: contracts.md §10: the product whose seeded stock is zero on purpose.
ZERO_STOCK_PRODUCT = "p-1005"


def available(api: httpx.Client, product_id: str) -> int:
    response = api.get(f"/api/products/{product_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["availableQuantity"])


def create_order(api: httpx.Client, *, customer_id: str, product_id: str = PRODUCT, quantity: int = 1):
    return api.post(
        "/api/orders",
        json={"customerId": customer_id, "lines": [{"productId": product_id, "quantity": quantity}]},
    )


def messages_for(emulator: httpx.Client, order_id: str, template: str) -> list[dict]:
    response = emulator.get("/messages", params={"orderId": order_id})
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item.get("template") == template]


def wait_for_message(emulator: httpx.Client, order_id: str, template: str, *, timeout: float = 60.0) -> list[dict]:
    """Wait until the deterministic notification of one order exists, then settle."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = messages_for(emulator, order_id, template)
        if found:
            time.sleep(2.0)  # a wrongly duplicated delivery would show up in this window
            return messages_for(emulator, order_id, template)
        time.sleep(0.5)
    pytest.fail(f"no '{template}' message arrived for order {order_id} within {timeout:g}s")


def deterministic_message_id(template: str, order_id: str) -> str:
    return "msg-" + hashlib.sha256(f"{template}:{order_id}".encode()).hexdigest()[:16]


def test_an_order_is_created_confirmed_and_announced_once(api: httpx.Client, emulator: httpx.Client) -> None:
    customer = f"e2e-create-{uuid.uuid4()}"
    before = available(api, PRODUCT)

    response = create_order(api, customer_id=customer, quantity=2)

    assert response.status_code == 201, response.text
    order = response.json()
    assert order["status"] == "CONFIRMED"
    assert order["confirmedAt"] is not None
    assert order["customerId"] == customer
    assert order["total"] > 0
    assert order["lines"][0]["productId"] == PRODUCT
    assert available(api, PRODUCT) == before - 2

    detail = api.get(f"/api/orders/{order['orderId']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "CONFIRMED"

    listed = api.get("/api/orders", params={"customerId": customer})
    assert listed.status_code == 200
    assert [item["orderId"] for item in listed.json()["items"]] == [order["orderId"]]

    confirmation = wait_for_message(emulator, order["orderId"], "order_confirmation")
    assert len(confirmation) == 1
    assert confirmation[0]["messageId"] == deterministic_message_id("order_confirmation", order["orderId"])


def test_the_zero_stock_product_is_rejected_without_creating_an_order(api: httpx.Client) -> None:
    customer = f"e2e-zero-{uuid.uuid4()}"

    response = create_order(api, customer_id=customer, product_id=ZERO_STOCK_PRODUCT, quantity=1)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    listed = api.get("/api/orders", params={"customerId": customer})
    assert listed.json() == {"items": [], "total": 0}
    assert available(api, ZERO_STOCK_PRODUCT) == 0


def test_a_cancellation_restores_the_stock_and_is_announced_once(api: httpx.Client, emulator: httpx.Client) -> None:
    customer = f"e2e-cancel-{uuid.uuid4()}"
    before = available(api, PRODUCT)
    order = create_order(api, customer_id=customer, quantity=1).json()
    assert available(api, PRODUCT) == before - 1
    reason = "customer changed their mind"

    response = api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": reason})

    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancelledAt"] is not None
    assert cancelled["cancellationReason"] == reason
    assert available(api, PRODUCT) == before

    cancellation = wait_for_message(emulator, order["orderId"], "order_cancellation")
    assert len(cancellation) == 1
    assert cancellation[0]["messageId"] == deterministic_message_id("order_cancellation", order["orderId"])

    repeated = api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": reason})
    assert repeated.status_code in {200, 409}
    if repeated.status_code == 200:
        assert repeated.json()["cancelledAt"] == cancelled["cancelledAt"]
    assert available(api, PRODUCT) == before  # no second restoration
    assert len(messages_for(emulator, order["orderId"], "order_cancellation")) == 1


def test_an_invalid_reason_is_rejected(api: httpx.Client) -> None:
    order = create_order(api, customer_id=f"e2e-bad-reason-{uuid.uuid4()}", quantity=1).json()

    empty = api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": "   "})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "invalid_reason"

    too_long = api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": "a" * 201})
    assert too_long.status_code == 400
    assert too_long.json()["error"]["code"] == "invalid_reason"

    assert api.get(f"/api/orders/{order['orderId']}").json()["status"] == "CONFIRMED"
    api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": "cleanup"})


def test_a_shipped_order_cannot_be_cancelled(api: httpx.Client) -> None:
    order = create_order(api, customer_id=f"e2e-ship-{uuid.uuid4()}", quantity=1).json()

    forbidden = api.post(f"/api/orders/{order['orderId']}/ship")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    shipped = api.post(f"/api/orders/{order['orderId']}/ship", headers={"X-ShopFlow-Role": "admin"})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["status"] == "SHIPPED"
    assert shipped.json()["shippedAt"] is not None

    cancelled = api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": "too late"})
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "order_shipped"
    assert api.get(f"/api/orders/{order['orderId']}").json()["status"] == "SHIPPED"


def test_shipping_a_cancelled_order_is_rejected(api: httpx.Client) -> None:
    order = create_order(api, customer_id=f"e2e-ship-cancelled-{uuid.uuid4()}", quantity=1).json()
    api.post(f"/api/orders/{order['orderId']}/cancel", json={"reason": "changed mind"})

    response = api.post(f"/api/orders/{order['orderId']}/ship", headers={"X-ShopFlow-Role": "admin"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_status_transition"


def test_an_unknown_order_is_not_found(api: httpx.Client) -> None:
    response = api.get(f"/api/orders/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_gateway_health_is_green(api: httpx.Client) -> None:
    for path in ("/health", "/ready"):
        response = api.get(path)
        assert response.status_code == 200, f"{path} -> {response.text}"


def test_the_infrastructure_smoke_test_passes(infra_scripts, gateway: str, provider: str) -> None:
    """shopflow-infra's own black-box smoke test must pass against this service."""

    completed = run_smoke_test(infra_scripts, gateway=gateway, provider=provider)

    assert completed.returncode == 0, (
        "the ecosystem smoke test failed\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    assert "OK" in completed.stdout.upper() or "PASS" in completed.stdout.upper()
