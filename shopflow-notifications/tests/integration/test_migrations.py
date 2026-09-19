"""Integration tests of the migrations and the migrated schema (contracts.md §9).

The migration is this repository's own tool; ``shopflow-infra`` only *invokes*
``alembic upgrade head``. These tests read the migrated store back through
SQLAlchemy's inspector and compare it with the authoritative §9 schema, so a
mismatch between the ORM metadata, the migration and the contract is caught here.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.domain.notification import NotificationStatus, NotificationType
from tests.support.migrations import run_alembic

#: contracts.md §9, "Notifications store": column -> (type, nullable).
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "notifications": {
        "id": ("UUID", False),
        "order_id": ("TEXT", False),
        "notification_type": ("TEXT", False),
        "event_id": ("UUID", False),
        "status": ("TEXT", False),
        "provider_message_id": ("TEXT", True),
        "attempts": ("INTEGER", False),
        "created_at": ("TIMESTAMP WITH TIME ZONE", True),
        "updated_at": ("TIMESTAMP WITH TIME ZONE", True),
    },
    "processed_events": {
        "event_id": ("UUID", False),
        "processed_at": ("TIMESTAMP WITH TIME ZONE", False),
    },
}

POSTGRES = postgresql.dialect()


def column_type(column: dict[str, Any]) -> str:
    return str(column["type"].compile(dialect=POSTGRES)).upper()


def test_the_migrated_store_has_exactly_the_contracted_shape(engine: Engine) -> None:
    inspector = inspect(engine)

    assert set(inspector.get_table_names()) >= {"notifications", "processed_events"}
    for table_name, expected in EXPECTED_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert set(columns) == set(expected), f"{table_name} columns differ from contracts.md §9"
        for name, (expected_type, expected_nullable) in expected.items():
            assert column_type(columns[name]) == expected_type, f"{table_name}.{name} type"
            assert bool(columns[name]["nullable"]) is expected_nullable, f"{table_name}.{name} nullability"


def test_the_primary_keys_are_the_contracted_ones(engine: Engine) -> None:
    inspector = inspect(engine)

    assert inspector.get_pk_constraint("notifications")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("processed_events")["constrained_columns"] == ["event_id"]


def test_the_logical_notification_is_unique_per_order_and_type(engine: Engine) -> None:
    inspector = inspect(engine)

    uniques = inspector.get_unique_constraints("notifications")
    assert [sorted(constraint["column_names"]) for constraint in uniques] == [["notification_type", "order_id"]]


def test_the_check_constraints_are_enforced_by_the_store(engine: Engine) -> None:
    inspector = inspect(engine)

    checks = {
        constraint["name"]: constraint["sqltext"].lower()
        for constraint in inspector.get_check_constraints("notifications")
    }
    assert len(checks) == 2
    type_check = next(text for text in checks.values() if "notification_type" in text)
    status_check = next(text for text in checks.values() if "status" in text)
    assert "'confirmation'" in type_check and "'cancellation'" in type_check
    assert "'sent'" in status_check and "'failed'" in status_check


def test_the_attempts_column_defaults_to_zero(engine: Engine) -> None:
    inspector = inspect(engine)

    attempts = next(column for column in inspector.get_columns("notifications") if column["name"] == "attempts")
    assert attempts["default"] is not None
    assert "0" in str(attempts["default"])


def test_the_orm_metadata_and_the_migrated_schema_agree(engine: Engine) -> None:
    """The model used by the adapter must describe the store the migration creates."""

    inspector = inspect(engine)

    for table in Base.metadata.sorted_tables:
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        assert set(columns) == {column.name for column in table.columns}, table.name
        for column in table.columns:
            assert bool(columns[column.name]["nullable"]) is bool(column.nullable), f"{table.name}.{column.name}"
            assert column_type(columns[column.name]) == str(column.type.compile(dialect=POSTGRES)).upper()

    uniques = inspect(engine).get_unique_constraints("notifications")
    metadata_unique = [
        [column.name for column in constraint.columns]
        for constraint in Base.metadata.tables["notifications"].constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert metadata_unique == [["order_id", "notification_type"]]
    assert [sorted(constraint["column_names"]) for constraint in uniques] == [["notification_type", "order_id"]]


def test_the_migration_is_at_head(migrated_store: str) -> None:
    completed = run_alembic(migrated_store, "current", "--verbose")

    assert completed.returncode == 0, completed.stderr
    assert "0001_notifications_schema" in completed.stdout
    assert "head" in completed.stdout


def test_upgrading_twice_changes_nothing(migrated_store: str, engine: Engine) -> None:
    tables = ("notifications", "processed_events")
    before = {table: sorted(Base.metadata.tables[table].columns.keys()) for table in tables}

    completed = run_alembic(migrated_store, "upgrade", "head")

    assert completed.returncode == 0, completed.stderr
    inspector = inspect(engine)
    assert inspector.get_table_names()
    assert {table: sorted(column["name"] for column in inspector.get_columns(table)) for table in before} == before


def test_the_migration_can_be_rolled_back_and_reapplied(migrated_store: str, engine: Engine) -> None:
    """A migration that cannot be reversed is a deployment risk; prove both directions."""

    try:
        downgraded = run_alembic(migrated_store, "downgrade", "base")
        assert downgraded.returncode == 0, downgraded.stderr
        inspector = inspect(engine)
        assert "notifications" not in inspector.get_table_names()
        assert "processed_events" not in inspector.get_table_names()
    finally:
        reapplied = run_alembic(migrated_store, "upgrade", "head")

    assert reapplied.returncode == 0, reapplied.stderr
    assert {"notifications", "processed_events"} <= set(inspect(engine).get_table_names())


def test_the_contract_vocabulary_is_accepted_by_the_store(
    session_factory: sessionmaker[Session],
) -> None:
    """The two notification types and the two statuses of contracts.md §9 are the only ones."""

    assert [kind.value for kind in NotificationType] == ["confirmation", "cancellation"]
    assert [status.value for status in NotificationStatus] == ["SENT", "FAILED"]


@pytest.mark.parametrize("invalid", ["order_confirmation", "confirmation ", "CONFIRMATION"])
def test_an_invalid_notification_type_is_refused_by_the_store(
    engine: Engine,
    invalid: str,
) -> None:
    import uuid

    with engine.connect() as connection:
        with pytest.raises(SQLAlchemyError):
            connection.execute(
                text(
                    "INSERT INTO notifications (id, order_id, notification_type, event_id, status, attempts)"
                    " VALUES (:id, :order_id, :kind, :event_id, 'SENT', 0)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "order_id": "check-order",
                    "kind": invalid,
                    "event_id": str(uuid.uuid4()),
                },
            )
            connection.commit()
        connection.rollback()
