"""Integration test of the diagnostic read over a real socket (acceptance criterion 7).

The whole stack runs behind uvicorn on a loopback port - real routing, real store,
real broker, real emulator, the consumer started by the application lifespan - and
the notification records are read back over HTTP, which is the path the ecosystem
smoke test and an operator use.
"""

from __future__ import annotations

import httpx

from app.adapters.provider_http_client import deterministic_message_id
from app.config import Settings
from app.main import create_app
from tests.support.events import event_id, order_cancelled, order_confirmed, order_id
from tests.support.runtimes import wait_until
from tests.support.servers import start_server

ITEM_FIELDS = {"orderId", "notificationType", "status", "providerMessageId", "createdAt"}


def test_the_recorded_notifications_are_readable_over_http(
    settings: Settings,
    engine,
    broker,
    emulator,
    case_id: str,
) -> None:
    order = order_id(f"{case_id}-order")
    confirmed_event = event_id(f"{case_id}-c")
    cancelled_event = event_id(f"{case_id}-x")
    app = create_app(settings=settings, engine=engine, start_consumer=True)

    with start_server(app, lifespan="on") as server, server.client() as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready").json()
        assert ready["status"] == "ready"
        assert ready["upstreams"] == {"store": True, "broker": True, "provider": True}
        assert ready["consumer"] == "running"

        broker.publish_event(order_confirmed(order=order, event=confirmed_event))
        broker.publish_event(
            order_cancelled(order=order, event=cancelled_event, reason="customer changed their mind")
        )

        def read() -> dict[str, object]:
            response = client.get("/notifications", params={"orderId": order})
            assert response.status_code == 200, response.text
            return response.json()

        wait_until(lambda: read()["total"] == 2, description="both notification records")
        body = read()

    assert body["total"] == 2
    items = sorted(body["items"], key=lambda item: str(item["notificationType"]))
    for item in items:
        assert set(item) == ITEM_FIELDS
        assert item["orderId"] == order
        assert item["status"] == "SENT"
        assert item["createdAt"].endswith("Z")
    assert [item["notificationType"] for item in items] == ["cancellation", "confirmation"]
    assert items[0]["providerMessageId"] == deterministic_message_id("order_cancellation", order)
    assert items[1]["providerMessageId"] == deterministic_message_id("order_confirmation", order)

    # exactly one message per notification type reached the provider
    assert emulator.count(order, "order_confirmation") == 1
    assert emulator.count(order, "order_cancellation") == 1
    assert emulator.count(order) == 2


def test_the_diagnostic_read_answers_the_error_envelope_over_http(
    settings: Settings,
    engine,
    case_id: str,
) -> None:
    app = create_app(settings=settings, engine=engine, start_consumer=False)

    with start_server(app) as server, httpx.Client(base_url=server.base_url, timeout=10.0) as client:
        missing = client.get("/notifications", headers={"X-Correlation-Id": "corr-http"})
        unknown_order = client.get("/notifications", params={"orderId": order_id(f"{case_id}-unknown")})

    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "validation_error"
    assert missing.json()["error"]["correlationId"] == "corr-http"
    assert missing.headers["X-Correlation-Id"] == "corr-http"

    assert unknown_order.status_code == 200
    assert unknown_order.json() == {"items": [], "total": 0}
