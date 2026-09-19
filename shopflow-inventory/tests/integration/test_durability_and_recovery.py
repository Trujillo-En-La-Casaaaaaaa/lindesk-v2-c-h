"""Integration tests: durability across restarts and readiness recovery.

Two properties of the handoff are proven here:

* the idempotency guarantee lives in the store, not in process memory: a retry
  handled by a *new* process instance (new engine, new session factory) returns
  the same logical outcome and never changes stock a second time;
* readiness follows the store: ``200`` when it answers, ``503`` when it does not,
  and the service recovers as soon as the store is reachable again.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.session import create_engine_from_settings
from app.main import create_app
from tests.support.db import movement_rows, stock_rows
from tests.support.requests import release_headers, reservation_body, reserve_headers, stock_of


def test_readiness_is_200_against_the_real_store(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_is_503_when_the_store_cannot_be_reached() -> None:
    unreachable = Settings(database_url="postgresql://shopflow:shopflow@127.0.0.1:1/shopflow_inventory")
    engine = create_engine_from_settings(unreachable)
    app = create_app(engine=engine)
    try:
        with TestClient(app) as unreachable_client:
            response = unreachable_client.get("/ready", headers={"X-Correlation-Id": "corr-down"})
    finally:
        engine.dispose()

    assert response.status_code == 503
    assert response.json()["error"]["correlationId"] == "corr-down"


def test_the_service_recovers_when_the_store_becomes_reachable(
    client: TestClient, settings: Settings
) -> None:
    """A 503 readiness probe is not sticky: the next probe reflects the store again."""

    down_engine = create_engine_from_settings(
        Settings(database_url="postgresql://shopflow:shopflow@127.0.0.1:1/shopflow_inventory")
    )
    try:
        with TestClient(create_app(engine=down_engine)) as down_client:
            assert down_client.get("/ready").status_code == 503
    finally:
        down_engine.dispose()

    assert client.get("/ready").status_code == 200
    assert client.get("/health").status_code == 200


def test_a_reservation_retry_after_a_restart_never_decrements_twice(
    client: TestClient, restarted_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    body = reservation_body("o-durable", ("p-1001", 2), ("p-1004", 5))
    headers = reserve_headers("o-durable")
    assert client.post("/inventory/reservations", json=body, headers=headers).status_code == 200
    before = stock_rows(session_factory)

    for _ in range(3):  # several retries, each handled by a freshly built instance
        replay = restarted_client.post("/inventory/reservations", json=body, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert stock_rows(session_factory) == before


def test_a_full_reserve_release_replay_cycle_after_restarts_ends_at_the_seeded_level(
    client: TestClient, restarted_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    seeded = stock_rows(session_factory)
    reserve_body = reservation_body("o-cycle", ("p-1002", 5))
    release_body = reservation_body("o-cycle", ("p-1002", 5))
    reserve_headers_ = reserve_headers("o-cycle")
    release_headers_ = release_headers("o-cycle")

    assert client.post("/inventory/reservations", json=reserve_body, headers=reserve_headers_).status_code == 200
    assert stock_rows(session_factory)["p-1002"] == 20

    # restart between the reservation and its release, then retry both commands
    assert restarted_client.post("/inventory/reservations", json=reserve_body, headers=reserve_headers_).json()[
        "replayed"
    ] is True
    first_release = restarted_client.post("/inventory/releases", json=release_body, headers=release_headers_)
    assert first_release.json() == {"orderId": "o-cycle", "restored": True, "replayed": False}
    assert stock_rows(session_factory)["p-1002"] == 25

    replayed_release = client.post("/inventory/releases", json=release_body, headers=release_headers_)
    assert replayed_release.json() == {"orderId": "o-cycle", "restored": False, "replayed": True}
    assert stock_rows(session_factory) == seeded
    assert len(movement_rows(session_factory, order_id="o-cycle")) == 2


def test_an_aborted_command_leaves_no_durable_trace(
    client: TestClient, restarted_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A rejected reservation must not leave a marker that hides a later retry."""

    headers = reserve_headers("o-abort")
    aborted = client.post(
        "/inventory/reservations", json=reservation_body("o-abort", ("p-1001", 2), ("p-1005", 1)), headers=headers
    )
    assert aborted.status_code == 409
    assert movement_rows(session_factory, order_id="o-abort") == []

    accepted = restarted_client.post(
        "/inventory/reservations", json=reservation_body("o-abort", ("p-1001", 2)), headers=headers
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"orderId": "o-abort", "reserved": True, "replayed": False}
    assert stock_of(restarted_client, "p-1001") == 48
    assert len(movement_rows(session_factory, order_id="o-abort")) == 1
