"""Contract tests of ``/health`` and ``/ready`` (contracts.md §5, HANDOFF #8)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support.fakes import FakeBroker, FakeInventory


def test_health_is_200_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_the_store_and_the_broker(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["upstreams"] == {"store": True, "broker": True, "inventory": True}


def test_ready_is_503_when_the_store_does_not_answer(client: TestClient, store_probe: dict[str, bool]) -> None:
    store_probe["reachable"] = False
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["upstreams"]["store"] is False


def test_ready_is_503_when_the_broker_is_unreachable(client: TestClient, broker: FakeBroker) -> None:
    broker.reachable = False
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["upstreams"]["broker"] is False


def test_an_unreachable_inventory_is_reported_without_gating_readiness(
    client: TestClient, inventory: FakeInventory
) -> None:
    """The two dependencies this service owns - store and broker - decide readiness."""

    inventory.make_unavailable("all")
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["upstreams"]["inventory"] is False
