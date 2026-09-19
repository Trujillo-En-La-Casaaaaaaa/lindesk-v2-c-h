"""Integration tests of the failure semantics (HANDOFF consistency table, criterion 11).

The failures that cannot be provoked through a healthy inventory service are produced
with a controllable stub on a real socket, while the store stays the real PostgreSQL
store and the orders application is the real ASGI application.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.inventory_http_client import InventoryHttpClient
from app.adapters.postgres_order_repository import PostgresOrderRepository
from app.adapters.postgres_outbox import PostgresOutbox
from tests.integration.conftest import make_reconciler, store_url
from tests.support.db import order_row as read_order_row
from tests.support.db import order_rows, outbox_rows
from tests.support.inventory_stub import (
    MODE_AVAILABILITY_DOWN,
    MODE_RELEASE_DOWN,
    MODE_RESERVE_AMBIGUOUS,
    MODE_RESERVE_DOWN,
    MODE_RESERVE_RACE_LOST,
)
from tests.support.servers import unused_port

REPO_ROOT = Path(__file__).resolve().parents[2]


def place(client: TestClient, *, product_id: str = "p-1001", quantity: int = 1, customer: str = "c-failure"):
    return client.post(
        "/orders",
        json={"customerId": customer, "lines": [{"productId": product_id, "quantity": quantity}]},
        headers={"X-Correlation-Id": "corr-failure"},
    )


def reconciler_for(session_factory: sessionmaker[Session], inventory_url: str):
    repository = PostgresOrderRepository(session_factory, PostgresOutbox(session_factory))
    inventory = InventoryHttpClient(inventory_url, timeout_seconds=5.0)
    return make_reconciler(repository, inventory)


# --------------------------------------------------------------------------- #
# acceptance criterion 11: an unreachable inventory writes no order
# --------------------------------------------------------------------------- #
def test_acceptance_11_a_refused_inventory_connection_writes_no_order(
    app_factory, session_factory: sessionmaker[Session]
) -> None:
    closed_port = unused_port()
    client = TestClient(app_factory(inventory_base_url=f"http://127.0.0.1:{closed_port}"))

    response = place(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"
    assert order_rows(session_factory) == []
    assert outbox_rows(session_factory) == []

    ready = client.get("/ready")
    assert ready.status_code == 200  # the store and the broker are fine
    assert ready.json()["upstreams"]["inventory"] is False


def test_acceptance_11_an_inventory_503_writes_no_order(app_factory, stub_inventory, session_factory) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_AVAILABILITY_DOWN)
    client = stub.client()

    response = place(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"
    assert order_rows(session_factory) == []
    assert stub.stock("p-1001") == 50  # nothing was touched


def test_an_exhausted_request_budget_is_reported_as_unavailable(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    """``REQUEST_TIMEOUT_SECONDS`` bounds the whole request, not just one call."""

    stub = stub_inventory()
    client = TestClient(app_factory(inventory_base_url=stub.base_url, request_timeout_seconds=0.000001))

    response = place(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"
    assert order_rows(session_factory) == []


# --------------------------------------------------------------------------- #
# crash windows of the create path
# --------------------------------------------------------------------------- #
def test_crash_after_the_order_commit_before_the_reservation_leaves_it_created(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_DOWN)
    client = stub.client()

    response = place(client, quantity=2)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"

    rows = order_rows(session_factory)
    assert len(rows) == 1
    assert rows[0]["status"] == "CREATED"
    assert rows[0]["confirmed_at"] is None
    assert outbox_rows(session_factory) == []  # a CREATED order is never reported confirmed

    detail = client.get(f"/orders/{rows[0]['id']}").json()
    assert detail["status"] == "CREATED"
    assert detail["confirmedAt"] is None
    assert stub.stock("p-1001") == 50  # nothing reserved


def test_the_reconciler_finishes_an_ambiguous_reservation_exactly_once(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    """The reservation happened but the answer was lost: the retry must not double-reserve."""

    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_AMBIGUOUS)
    client = stub.client()

    response = place(client, quantity=2)
    assert response.status_code == 503

    order_id = order_rows(session_factory)[0]["id"]
    assert order_rows(session_factory)[0]["status"] == "CREATED"
    assert stub.stock("p-1001") == 48  # the reservation really happened
    assert outbox_rows(session_factory) == []

    report = reconciler_for(session_factory, stub.base_url).run_once()

    assert report.confirmed == 1
    row = read_order_row(session_factory, order_id)
    assert row is not None
    assert row["status"] == "CONFIRMED"
    assert row["confirmed_at"] is not None
    assert stub.stock("p-1001") == 48  # the replayed reservation reserved nothing again
    assert len(stub.movements(order_id)) == 1
    events = outbox_rows(session_factory, order_id=order_id)
    assert [event["event_type"] for event in events] == ["order.confirmed"]

    # a second pass must not confirm (or publish) a second time
    assert reconciler_for(session_factory, stub.base_url).run_once().confirmed == 0
    assert len(outbox_rows(session_factory, order_id=order_id)) == 1


def test_a_lost_reservation_race_marks_the_order_failed(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    stub = stub_inventory()
    stub.state.set_mode(MODE_RESERVE_RACE_LOST)
    client = stub.client()

    response = place(client, quantity=1)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    rows = order_rows(session_factory)
    assert [row["status"] for row in rows] == ["FAILED"]
    assert outbox_rows(session_factory) == []
    assert stub.state.movements_for(rows[0]["id"]) == []  # all-or-nothing: nothing reserved
    assert stub.stock("p-1001") == 50

    # nothing for the reconciler to repair: a FAILED order is terminal
    report = reconciler_for(session_factory, stub.base_url).run_once()
    assert (report.confirmed, report.failed, report.released) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# durable cancellation
# --------------------------------------------------------------------------- #
def test_a_failed_release_stays_pending_and_is_repaired(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    stub = stub_inventory()
    client = stub.client()
    created = place(client, quantity=2, customer="c-release")
    assert created.status_code == 201
    order_id = created.json()["orderId"]

    stub.state.set_mode(MODE_RELEASE_DOWN)
    cancelled = client.post(f"/orders/{order_id}/cancel", json={"reason": "customer changed their mind"})

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    row = read_order_row(session_factory, order_id)
    assert row is not None
    assert row["status"] == "CANCELLED"
    assert row["inventory_release_state"] == "PENDING"
    assert stub.stock("p-1001") == 48  # not restored yet
    assert [movement["operation"] for movement in stub.movements(order_id)] == ["reserve"]

    stub.state.set_mode("normal")
    report = reconciler_for(session_factory, stub.base_url).run_once()

    assert report.released == 1
    row = read_order_row(session_factory, order_id)
    assert row is not None and row["inventory_release_state"] == "RELEASED"
    assert stub.stock("p-1001") == 50  # restored exactly once
    assert [movement["operation"] for movement in stub.movements(order_id)] == ["reserve", "release"]


def test_the_reconciler_does_not_restore_twice_over_repeated_passes(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    stub = stub_inventory()
    client = stub.client()
    created = place(client, quantity=3, customer="c-release-repeat")
    assert created.status_code == 201
    order_id = created.json()["orderId"]
    stub.state.set_mode(MODE_RELEASE_DOWN)
    client.post(f"/orders/{order_id}/cancel", json={"reason": "changed mind"})

    stub.state.set_mode("normal")
    worker = reconciler_for(session_factory, stub.base_url)
    reports = [worker.run_once() for _ in range(3)]

    assert reports[0].released == 1
    assert all(report.released == 0 for report in reports[1:])
    assert stub.stock("p-1001") == 50
    assert [movement["operation"] for movement in stub.movements(order_id)] == ["reserve", "release"]
    assert len(stub.state.calls_of("release")) >= 1


def test_the_reconciler_worker_repairs_a_pending_release_in_a_separate_process(
    app_factory, stub_inventory, session_factory: sessionmaker[Session]
) -> None:
    """A killed worker still leaves durable progress: a *new process* finishes the job."""

    stub = stub_inventory()
    client = stub.client()
    created = place(client, quantity=2, customer="c-release-worker")
    assert created.status_code == 201
    order_id = created.json()["orderId"]
    stub.state.set_mode(MODE_RELEASE_DOWN)
    client.post(f"/orders/{order_id}/cancel", json={"reason": "changed mind"})
    assert read_order_row(session_factory, order_id)["inventory_release_state"] == "PENDING"

    stub.state.set_mode("normal")
    environment = {
        **os.environ,
        "DATABASE_URL": store_url(),
        "INVENTORY_BASE_URL": stub.base_url,
        "RELEASE_RETRY_INTERVAL_SECONDS": "0.2",
        "RESERVATION_GRACE_SECONDS": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and fixed module
        [sys.executable, "-m", "app.workers.release_reconciler"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state: str | None = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = read_order_row(session_factory, order_id)["inventory_release_state"]
            if state == "RELEASED":
                break
            time.sleep(0.2)
    finally:
        process.kill()  # a hard kill, like a crash
        process.wait(timeout=30)

    assert state == "RELEASED", "the worker did not repair the pending release"
    assert stub.stock("p-1001") == 50
    assert [movement["operation"] for movement in stub.movements(order_id)] == ["reserve", "release"]
