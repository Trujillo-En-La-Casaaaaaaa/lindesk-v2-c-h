"""``/health`` and ``/ready`` (contracts.md §2)."""

from __future__ import annotations

import httpx

from tests.support.assertions import assert_error_envelope, assert_json_response


def test_health_is_liveness_only(client: httpx.Client, evidence: object) -> None:
    response = client.get("/health")

    body = assert_json_response(response, status=200)
    assert body == {"status": "ok"}
    assert evidence.gateway_calls() == 0  # type: ignore[attr-defined]
    assert evidence.upstream_calls("inventory") == 0  # type: ignore[attr-defined]
    assert evidence.upstream_calls("orders") == 0  # type: ignore[attr-defined]


def test_ready_reports_both_upstreams(client: httpx.Client, evidence: object) -> None:
    response = client.get("/ready")

    body = assert_json_response(response, status=200)
    assert body == {"status": "ready", "upstreams": {"orders": "ok", "inventory": "ok"}}
    assert evidence.upstream_calls("orders", "GET", "/health") == 1  # type: ignore[attr-defined]
    assert evidence.upstream_calls("inventory", "GET", "/health") == 1  # type: ignore[attr-defined]


def test_ready_is_503_when_inventory_is_down(gateway_factory: object, stopped_upstream_port: int) -> None:
    gateway = gateway_factory(inventory_base_url=f"http://127.0.0.1:{stopped_upstream_port}")  # type: ignore[operator]

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/ready")

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"] == {"upstreams": {"orders": "ok", "inventory": "unavailable"}}


def test_ready_is_503_when_orders_is_down(gateway_factory: object, stopped_upstream_port: int) -> None:
    gateway = gateway_factory(orders_base_url=f"http://127.0.0.1:{stopped_upstream_port}")  # type: ignore[operator]

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/ready")

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"] == {"upstreams": {"orders": "unavailable", "inventory": "ok"}}


def test_ready_is_503_when_both_upstreams_are_down(gateway_factory: object, stopped_upstream_port: int) -> None:
    gateway = gateway_factory(  # type: ignore[operator]
        orders_base_url=f"http://127.0.0.1:{stopped_upstream_port}",
        inventory_base_url=f"http://127.0.0.1:{stopped_upstream_port}",
    )

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/ready")

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"] == {"upstreams": {"orders": "unavailable", "inventory": "unavailable"}}


def test_ready_is_503_when_an_upstream_health_endpoint_is_not_ok(
    gateway_factory: object,
    upstream_stack: object,
) -> None:
    upstream_stack.faults_for("inventory").add(  # type: ignore[attr-defined]
        "GET",
        "/health",
        status=503,
        body={"status": "starting"},
        times=None,
    )
    gateway = gateway_factory()  # type: ignore[operator]

    with httpx.Client(base_url=gateway.base_url, timeout=15.0) as client:
        response = client.get("/ready")

    error = assert_error_envelope(response, status=503, code="upstream_unavailable")
    assert error["details"]["upstreams"]["inventory"] == "unavailable"
