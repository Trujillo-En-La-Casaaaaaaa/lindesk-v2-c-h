"""Contract tests for the deterministic notification-provider emulator (contracts.md §7).

These tests are self-contained: they exercise the ASGI application in process, so
they pass with no container, no network, and no other ShopFlow service running.
"""

from __future__ import annotations

import hashlib
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.store import MessageStore, deterministic_message_id

CONFIRMATION = "order_confirmation"
CANCELLATION = "order_cancellation"


def order_id() -> str:
    return str(uuid.uuid4())


def message_body(order: str, template: str = CONFIRMATION, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel": "email",
        "to": "customer@shopflow.test",
        "template": template,
        "orderId": order,
        "payload": {"orderId": order, "total": 19.99, "lines": []},
    }
    body.update(overrides)
    return body


def post_message(
    client: TestClient,
    order: str,
    template: str = CONFIRMATION,
    *,
    key: str | None = None,
    body: dict[str, Any] | None = None,
):
    return client.post(
        "/messages",
        json=body if body is not None else message_body(order, template),
        headers={"Idempotency-Key": key if key is not None else f"order:{order}:{template}"},
    )


# --------------------------------------------------------------------------- #
# messageId — deterministic, exactly as specified by contracts.md §7
# --------------------------------------------------------------------------- #
def test_message_id_matches_the_contract_formula() -> None:
    order = order_id()
    expected = "msg-" + hashlib.sha256(f"{CONFIRMATION}:{order}".encode("utf-8")).hexdigest()[:16]

    assert deterministic_message_id(CONFIRMATION, order) == expected
    assert deterministic_message_id(CONFIRMATION, order) == expected
    assert len(expected) == len("msg-") + 16


def test_message_id_is_stable_and_template_specific(client: TestClient) -> None:
    order = order_id()
    confirmation = post_message(client, order, CONFIRMATION)
    cancellation = post_message(client, order, CANCELLATION)

    assert confirmation.json()["messageId"] == deterministic_message_id(CONFIRMATION, order)
    assert cancellation.json()["messageId"] == deterministic_message_id(CANCELLATION, order)
    assert confirmation.json()["messageId"] != cancellation.json()["messageId"]


def test_message_id_is_identical_for_two_independent_stores() -> None:
    order = order_id()
    first, _ = MessageStore().accept(
        idempotency_key=f"order:{order}:{CONFIRMATION}",
        order_id=order,
        template=CONFIRMATION,
        to="a@shopflow.test",
        channel="email",
        payload={},
    )
    second, _ = MessageStore().accept(
        idempotency_key=f"order:{order}:{CONFIRMATION}",
        order_id=order,
        template=CONFIRMATION,
        to="b@shopflow.test",
        channel="email",
        payload={"different": "body"},
    )

    assert first.message_id == second.message_id == deterministic_message_id(CONFIRMATION, order)


# --------------------------------------------------------------------------- #
# POST /messages
# --------------------------------------------------------------------------- #
def test_accept_returns_the_documented_success_body(client: TestClient) -> None:
    order = order_id()
    response = post_message(client, order)

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"messageId", "status", "acceptedAt"}
    assert payload["status"] == "accepted"
    assert payload["messageId"] == deterministic_message_id(CONFIRMATION, order)
    assert payload["acceptedAt"].endswith("Z")


def test_repeated_key_returns_the_original_message_and_records_one(client: TestClient) -> None:
    order = order_id()
    first = post_message(client, order)
    second = post_message(client, order)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json(), "a replayed key must return the original outcome"
    assert store.count(order) == 1

    recorded = client.get("/messages", params={"orderId": order}).json()
    assert recorded["total"] == 1


def test_repeated_key_with_a_different_body_is_a_conflict(client: TestClient) -> None:
    order = order_id()
    key = f"order:{order}:{CONFIRMATION}"
    first = post_message(client, order, key=key)
    second = post_message(
        client,
        order,
        key=key,
        body=message_body(order, to="someone-else@shopflow.test"),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "idempotency_conflict"
    assert error["correlationId"]
    assert store.count(order, CONFIRMATION) == 1


def test_concurrent_identical_replays_record_exactly_one_message() -> None:
    """Eight concurrent sends of the same key/body must yield one message (§7).

    The smoke test asserts "exactly one confirmation/cancellation message"; this
    proves the emulator keeps that promise when the same key arrives on several
    connections at once, which is exactly what an at-least-once consumer retry
    looks like.
    """

    order = order_id()
    body = message_body(order)
    key = f"order:{order}:{CONFIRMATION}"
    expected_id = deterministic_message_id(CONFIRMATION, order)

    def send(_: int) -> tuple[int, str | None]:
        response = TestClient(app).post(
            "/messages",
            json=body,
            headers={"Idempotency-Key": key},
        )
        payload = response.json() if response.status_code == 200 else {}
        return response.status_code, payload.get("messageId")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(8)))

    assert {status for status, _ in results} == {200}
    assert {message_id for _, message_id in results} == {expected_id}
    assert store.count(order, CONFIRMATION) == 1
    assert store.count() == 1


def test_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    order = order_id()
    response = client.post("/messages", json=message_body(order))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert store.count() == 0


def test_blank_idempotency_key_is_rejected(client: TestClient) -> None:
    order = order_id()
    response = client.post(
        "/messages",
        json=message_body(order),
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert store.count() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"template": "order_refunded"},
        {"template": ""},
        {"channel": "sms"},
        {"orderId": "   "},
        {"to": ""},
        {"payload": "not-an-object"},
    ],
)
def test_invalid_bodies_are_rejected_with_the_error_envelope(
    client: TestClient, overrides: dict[str, Any]
) -> None:
    order = order_id()
    body = message_body(order, **overrides)
    response = client.post("/messages", json=body, headers={"Idempotency-Key": f"order:{order}:x"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "correlationId" in error
    assert store.count() == 0


def test_cancellation_message_keeps_the_reason_payload(client: TestClient) -> None:
    order = order_id()
    body = message_body(order, CANCELLATION, payload={"orderId": order, "reason": "changed my mind"})
    response = post_message(client, order, CANCELLATION, body=body)

    assert response.status_code == 200
    recorded = client.get("/messages", params={"orderId": order}).json()["items"]
    assert [item["template"] for item in recorded] == [CANCELLATION]


# --------------------------------------------------------------------------- #
# GET /messages
# --------------------------------------------------------------------------- #
def test_get_messages_filters_by_order_id(client: TestClient) -> None:
    first, second = order_id(), order_id()
    post_message(client, first, CONFIRMATION)
    post_message(client, first, CANCELLATION)
    post_message(client, second, CONFIRMATION)

    everything = client.get("/messages").json()
    assert everything["total"] == 3

    only_first = client.get("/messages", params={"orderId": first}).json()
    assert only_first["total"] == 2
    assert {item["template"] for item in only_first["items"]} == {CONFIRMATION, CANCELLATION}
    assert all(item["orderId"] == first for item in only_first["items"])
    assert {"messageId", "orderId", "template", "to", "receivedAt"} == set(only_first["items"][0])


def test_get_messages_returns_an_empty_list_for_an_unknown_order(client: TestClient) -> None:
    response = client.get("/messages", params={"orderId": order_id()})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


# --------------------------------------------------------------------------- #
# Health, readiness, errors, correlation
# --------------------------------------------------------------------------- #
def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/ready").json()["status"] == "ready"


def test_correlation_id_is_echoed_when_supplied(client: TestClient) -> None:
    order = order_id()
    response = client.post(
        "/messages",
        json=message_body(order),
        headers={"Idempotency-Key": f"order:{order}:{CONFIRMATION}", "X-Correlation-Id": "corr-123"},
    )

    assert response.headers["X-Correlation-Id"] == "corr-123"


def test_correlation_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/health")

    generated = response.headers.get("X-Correlation-Id")
    assert generated
    assert generated.strip() == generated


def test_error_envelope_carries_the_correlation_id(client: TestClient) -> None:
    order = order_id()
    response = client.post(
        "/messages",
        json=message_body(order, template="nope"),
        headers={"Idempotency-Key": f"order:{order}:x", "X-Correlation-Id": "corr-xyz"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["correlationId"] == "corr-xyz"


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Offline guarantee
# --------------------------------------------------------------------------- #
def test_the_emulator_exposes_exactly_the_contract_routes() -> None:
    """contracts.md §7 defines four routes; the emulator must expose no more.

    The interactive documentation is disabled on purpose, so the HTTP surface *is*
    the contract — this asserts that no extra route (or doc route) slipped in.
    """

    exposed = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert exposed == {
        ("/messages", "POST"),
        ("/messages", "GET"),
        ("/health", "GET"),
        ("/ready", "GET"),
    }
    assert {route.path for route in app.routes} == {"/messages", "/health", "/ready"}


def test_accepted_messages_never_dial_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emulator must not depend on the internet (contracts.md §7).

    Every real outbound HTTP client (httpx, urllib, requests, ...) has to resolve
    a name and open a connection, so disabling those two primitives proves that
    accepting and reading a message never leaves the process. ``socketpair``
    (used internally by the test client's own event loop) is left alone.
    """

    def forbidden(*_args: Any, **_kwargs: Any):  # pragma: no cover - only runs on failure
        raise AssertionError("the provider emulator must not open an outbound connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    order = order_id()
    assert post_message(client, order).status_code == 200
    assert post_message(client, order).status_code == 200
    assert client.get("/messages", params={"orderId": order}).json()["total"] == 1
