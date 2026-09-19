"""Integration test: the migrated store really is the schema of contracts.md §9."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from tests.integration.conftest import alembic, store_url
from tests.support.db import (
    check_constraints,
    foreign_keys,
    table_columns,
    unique_constraints,
)

ORDERS_COLUMNS = {
    "id",
    "customer_id",
    "status",
    "inventory_release_state",
    "total",
    "created_at",
    "updated_at",
    "confirmed_at",
    "shipped_at",
    "cancelled_at",
    "cancellation_reason",
}

ORDER_LINES_COLUMNS = {"id", "order_id", "product_id", "sku", "name", "quantity", "unit_price"}

OUTBOX_COLUMNS = {"id", "event_id", "event_type", "payload", "occurred_at", "published_at", "attempts"}


def test_the_three_tables_match_the_documented_columns(session_factory: sessionmaker[Session]) -> None:
    assert set(table_columns(session_factory, "orders")) == ORDERS_COLUMNS
    assert set(table_columns(session_factory, "order_lines")) == ORDER_LINES_COLUMNS
    assert set(table_columns(session_factory, "outbox")) == OUTBOX_COLUMNS


def test_orders_columns_have_the_documented_types(session_factory: sessionmaker[Session]) -> None:
    columns = table_columns(session_factory, "orders")

    assert columns["id"]["data_type"] == "uuid"
    assert columns["customer_id"]["data_type"] == "text"
    assert columns["status"]["data_type"] == "text"
    assert columns["status"]["is_nullable"] == "NO"
    assert columns["inventory_release_state"]["data_type"] == "text"
    assert columns["inventory_release_state"]["is_nullable"] == "NO"
    assert "NONE" in str(columns["inventory_release_state"]["column_default"])
    assert columns["total"]["data_type"] == "numeric"
    assert columns["total"]["is_nullable"] == "NO"
    assert columns["created_at"]["data_type"] == "timestamp with time zone"
    assert columns["cancellation_reason"]["data_type"] == "character varying"
    assert columns["cancellation_reason"]["character_maximum_length"] == 200
    assert columns["confirmed_at"]["is_nullable"] == "YES"
    assert columns["shipped_at"]["is_nullable"] == "YES"
    assert columns["cancelled_at"]["is_nullable"] == "YES"


def test_order_lines_and_outbox_columns_have_the_documented_types(session_factory: sessionmaker[Session]) -> None:
    lines = table_columns(session_factory, "order_lines")
    assert lines["quantity"]["data_type"] == "integer"
    assert lines["quantity"]["is_nullable"] == "NO"
    assert lines["unit_price"]["data_type"] == "numeric"
    assert lines["sku"]["data_type"] == "text"
    assert lines["name"]["data_type"] == "text"

    outbox = table_columns(session_factory, "outbox")
    assert outbox["event_id"]["data_type"] == "uuid"
    assert outbox["event_type"]["data_type"] == "text"
    assert outbox["payload"]["data_type"] == "jsonb"
    assert outbox["occurred_at"]["is_nullable"] == "NO"
    assert outbox["published_at"]["is_nullable"] == "YES"
    assert "0" in str(outbox["attempts"]["column_default"])


def test_the_status_and_release_state_checks_are_in_the_database(session_factory: sessionmaker[Session]) -> None:
    checks = check_constraints(session_factory, "orders")
    status = checks["ck_orders_status"]
    for value in ("CREATED", "CONFIRMED", "FAILED", "SHIPPED", "CANCELLED"):
        assert value in status
    release = checks["ck_orders_inventory_release_state"]
    for value in ("NONE", "PENDING", "RELEASED"):
        assert value in release


def test_the_other_structural_constraints_are_in_the_database(session_factory: sessionmaker[Session]) -> None:
    line_checks = check_constraints(session_factory, "order_lines")
    assert "quantity > 0" in line_checks["ck_order_lines_quantity_positive"].replace("(", "").replace(")", "")

    uniques = unique_constraints(session_factory, "outbox")
    assert any("event_id" in definition for definition in uniques.values())

    assert any("orders(id)" in definition for definition in foreign_keys(session_factory, "order_lines"))


def test_the_orm_metadata_agrees_with_the_migrated_schema(session_factory: sessionmaker[Session]) -> None:
    for table in ("orders", "order_lines", "outbox"):
        database_columns = set(table_columns(session_factory, table))
        model_columns = set(Base.metadata.tables[table].columns.keys())
        assert database_columns == model_columns, f"{table} drifted between models and migration"


def test_the_migration_is_applied_and_idempotent(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        version = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0001_orders_schema"

    again = alembic("upgrade", "head")
    assert again.returncode == 0, again.stderr
    with session_factory() as session:
        assert session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001_orders_schema"


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            "INSERT INTO orders (id, customer_id, status, total, created_at, updated_at) "
            "VALUES (:id, 'c-1', 'BOGUS', 1.00, now(), now())",
            {},
        ),
        (
            "INSERT INTO orders (id, customer_id, status, inventory_release_state, total, created_at, updated_at) "
            "VALUES (:id, 'c-1', 'CREATED', 'MAYBE', 1.00, now(), now())",
            {},
        ),
    ],
)
def test_the_database_refuses_an_invalid_state(
    session_factory: sessionmaker[Session], statement: str, parameters: dict
) -> None:
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(statement), {"id": str(uuid.uuid4()), **parameters})
            session.flush()
        session.rollback()


def test_the_database_refuses_a_non_positive_line_quantity(session_factory: sessionmaker[Session]) -> None:
    order_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO orders (id, customer_id, status, total, created_at, updated_at) "
                "VALUES (:id, 'c-1', 'CREATED', 1.00, :now, :now)"
            ),
            {"id": order_id, "now": now},
        )
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO order_lines (id, order_id, product_id, sku, name, quantity, unit_price) "
                    "VALUES (:line_id, :order_id, 'p-1001', 'SF-TEE-001', 'Tee', 0, 19.99)"
                ),
                {"line_id": str(uuid.uuid4()), "order_id": order_id},
            )
            session.flush()
        session.rollback()


def test_the_database_refuses_a_duplicate_event_id(session_factory: sessionmaker[Session]) -> None:
    event_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    insert = text(
        "INSERT INTO outbox (id, event_id, event_type, payload, occurred_at) "
        "VALUES (:id, :event_id, 'order.confirmed', '{}'::jsonb, :now)"
    )
    with session_factory() as session:
        session.execute(insert, {"id": str(uuid.uuid4()), "event_id": event_id, "now": now})
        with pytest.raises(IntegrityError):
            session.execute(insert, {"id": str(uuid.uuid4()), "event_id": event_id, "now": now})
            session.flush()
        session.rollback()


def test_the_store_is_the_one_named_by_database_url() -> None:
    """The suite really talks to the store the service is configured with."""

    assert store_url() in candidate_database_urls_for_test()


def candidate_database_urls_for_test() -> list[str]:
    from tests.integration.conftest import candidate_database_urls

    return candidate_database_urls()
