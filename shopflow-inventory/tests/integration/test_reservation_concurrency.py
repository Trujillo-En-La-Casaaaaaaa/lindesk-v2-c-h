"""Integration tests: the concurrency invariant against PostgreSQL (acceptance 3).

These tests run the real application behind a real HTTP server and fire competing
requests at the same instant. They assert the three things the handoff requires:
exactly one winner, the loser getting ``409 insufficient_stock`` (never a database
error), and a ledger that is still at zero - never negative.
"""

from __future__ import annotations

from functools import partial

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.postgres_stock import PostgresStockRepository
from app.domain.errors import InsufficientStock
from tests.support.db import movement_rows, stock_rows
from tests.support.requests import (
    RELEASE,
    RESERVE,
    fire_concurrently,
    release_body,
    release_headers,
    reservation_body,
    reserve_headers,
    stock_of,
)


@pytest.fixture()
def http(server) -> httpx.Client:
    with server.client() as client:
        yield client


def reserve(client: httpx.Client, order_id: str, *lines: tuple[str, int]) -> httpx.Response:
    return client.post(RESERVE, json=reservation_body(order_id, *lines), headers=reserve_headers(order_id))


def release(client: httpx.Client, order_id: str, *lines: tuple[str, int]) -> httpx.Response:
    return client.post(RELEASE, json=release_body(order_id, *lines), headers=release_headers(order_id))


def attempt(client: httpx.Client, order_id: str, lines: tuple[tuple[str, int], ...]) -> tuple[str, httpx.Response]:
    """A reservation attempt that also reports which order it belongs to."""

    return order_id, reserve(client, order_id, *lines)


def test_two_concurrent_orders_racing_for_the_last_unit(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    assert reserve(http, "o-fill", ("p-1003", 9)).status_code == 200
    assert stock_of(http, "p-1003") == 1

    results = fire_concurrently(
        [
            partial(attempt, http, "o-race-a", (("p-1003", 1),)),
            partial(attempt, http, "o-race-b", (("p-1003", 1),)),
        ]
    )

    winners = [(order_id, response) for order_id, response in results if response.status_code == 200]
    losers = [(order_id, response) for order_id, response in results if response.status_code != 200]

    assert len(winners) == 1, [response.text for _order, response in results]
    assert winners[0][1].json() == {"orderId": winners[0][0], "reserved": True, "replayed": False}
    assert len(losers) == 1
    assert losers[0][1].status_code == 409
    assert losers[0][1].json()["error"]["code"] == "insufficient_stock"
    # the loser left nothing behind
    assert movement_rows(session_factory, order_id=losers[0][0]) == []
    assert stock_of(http, "p-1003") == 0
    assert stock_rows(session_factory)["p-1003"] == 0
    assert len(movement_rows(session_factory, operation="reserve")) == 2  # the fill plus the winner


def test_seven_concurrent_reservations_never_oversell(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    """10 units, seven orders asking for 2: exactly five may win."""

    results = fire_concurrently(
        [partial(reserve, http, f"o-burst-{index}", ("p-1003", 2)) for index in range(7)]
    )

    succeeded = [response for response in results if response.status_code == 200]
    rejected = [response for response in results if response.status_code == 409]

    assert len(succeeded) == 5, [(response.status_code, response.text) for response in results]
    assert len(rejected) == 2
    assert all(response.json()["error"]["code"] == "insufficient_stock" for response in rejected)
    assert not [response for response in results if response.status_code >= 500]
    assert stock_of(http, "p-1003") == 0
    assert stock_rows(session_factory)["p-1003"] == 0
    assert len(movement_rows(session_factory, operation="reserve")) == 5


def test_concurrent_retries_of_the_same_reservation_decrement_once(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    """The ambiguous-failure case: four identical in-flight retries, one decrement."""

    results = fire_concurrently([partial(reserve, http, "o-retried", ("p-1001", 2)) for _ in range(4)])

    assert {response.status_code for response in results} == {200}
    payloads = [response.json() for response in results]
    assert [payload["reserved"] for payload in payloads] == [True] * 4
    assert [payload["replayed"] for payload in payloads].count(False) == 1
    assert stock_of(http, "p-1001") == 48
    assert len(movement_rows(session_factory, order_id="o-retried")) == 1


def test_concurrent_retries_of_the_same_release_restore_once(
    http: httpx.Client, session_factory: sessionmaker[Session]
) -> None:
    assert reserve(http, "o-released", ("p-1001", 2)).status_code == 200

    results = fire_concurrently([partial(release, http, "o-released", ("p-1001", 2)) for _ in range(4)])

    assert {response.status_code for response in results} == {200}
    payloads = [response.json() for response in results]
    assert [payload["restored"] for payload in payloads].count(True) == 1
    assert [payload["replayed"] for payload in payloads].count(True) == 3
    assert stock_of(http, "p-1001") == 50
    assert len(movement_rows(session_factory, order_id="o-released", operation="release")) == 1


def test_competing_reservations_with_reversed_lines_do_not_deadlock(http: httpx.Client) -> None:
    """Two orders touching the same products in opposite line order still both commit."""

    for round_index in range(8):
        results = fire_concurrently(
            [
                partial(reserve, http, f"o-order-a-{round_index}", ("p-1001", 1), ("p-1002", 1)),
                partial(reserve, http, f"o-order-b-{round_index}", ("p-1002", 1), ("p-1001", 1)),
            ]
        )
        assert [response.status_code for response in results] == [200, 200], [response.text for response in results]

    assert stock_of(http, "p-1001") == 34
    assert stock_of(http, "p-1002") == 9


def test_the_store_check_constraint_surfaces_as_insufficient_stock(session_factory: sessionmaker[Session]) -> None:
    """A write that would go negative must not leak a database error to the caller."""

    repository = PostgresStockRepository(session_factory)

    with pytest.raises(InsufficientStock) as error, repository.transaction() as transaction:
        transaction.lock_levels(["p-1002"])
        transaction.decrement({"p-1002": 10_000})

    assert error.value.code == "insufficient_stock"
    assert stock_rows(session_factory)["p-1002"] == 25
