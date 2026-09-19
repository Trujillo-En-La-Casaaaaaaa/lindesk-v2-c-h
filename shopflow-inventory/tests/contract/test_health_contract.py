"""Contract tests: ``/health`` and ``/ready`` (contracts.md §4, handoff change 7)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_alive(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_is_200_when_the_store_answers(ready_client: TestClient) -> None:
    response = ready_client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_is_503_when_the_store_is_unreachable(unreachable_store_client: TestClient) -> None:
    response = unreachable_store_client.get("/ready", headers={"X-Correlation-Id": "corr-ready"})

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["correlationId"] == "corr-ready"


def test_health_still_answers_when_the_store_is_unreachable(unreachable_store_client: TestClient) -> None:
    """Liveness must not depend on the store, otherwise a restart loop hides a store outage."""

    assert unreachable_store_client.get("/health").status_code == 200
