"""Contract tests of the owned schema (contracts.md §9, "Notifications store").

The ORM metadata is compared with the authoritative table definitions of
contracts.md §9: the two tables, every column with its nullable flag, the unique
constraint on ``(order_id, notification_type)``, the two check constraints and the
two primary keys - and nothing else. The integration suite repeats this against the
migrated store itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import CheckConstraint, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects import postgresql

from app.db.models import Base, NotificationRow, ProcessedEventRow

#: contracts.md §9 - the exact column set with its nullability.
EXPECTED_COLUMNS: dict[str, dict[str, bool]] = {
    "notifications": {
        "id": False,
        "order_id": False,
        "notification_type": False,
        "event_id": False,
        "status": False,
        "provider_message_id": True,
        "attempts": False,
        "created_at": True,
        "updated_at": True,
    },
    "processed_events": {
        "event_id": False,
        "processed_at": False,
    },
}

POSTGRES = postgresql.dialect()


def compile_type(column: Any) -> str:
    return str(column.type.compile(dialect=POSTGRES)).upper()


def test_the_store_owns_exactly_the_two_contracted_tables() -> None:
    assert set(Base.metadata.tables) == {"notifications", "processed_events"}


@pytest.mark.parametrize("table_name", sorted(EXPECTED_COLUMNS))
def test_every_table_has_exactly_the_contracted_columns(table_name: str) -> None:
    table = Base.metadata.tables[table_name]

    assert {column.name: column.nullable for column in table.columns} == EXPECTED_COLUMNS[table_name]


def test_the_column_types_are_the_contracted_ones() -> None:
    notifications = Base.metadata.tables["notifications"]
    processed = Base.metadata.tables["processed_events"]

    assert compile_type(notifications.c.id) == "UUID"
    assert compile_type(notifications.c.order_id) == "TEXT"
    assert compile_type(notifications.c.notification_type) == "TEXT"
    assert compile_type(notifications.c.event_id) == "UUID"
    assert compile_type(notifications.c.status) == "TEXT"
    assert compile_type(notifications.c.provider_message_id) == "TEXT"
    assert compile_type(notifications.c.attempts) == "INTEGER"
    assert compile_type(notifications.c.created_at) == "TIMESTAMP WITH TIME ZONE"
    assert compile_type(notifications.c.updated_at) == "TIMESTAMP WITH TIME ZONE"

    assert compile_type(processed.c.event_id) == "UUID"
    assert compile_type(processed.c.processed_at) == "TIMESTAMP WITH TIME ZONE"


def test_the_attempts_column_defaults_to_zero() -> None:
    attempts = Base.metadata.tables["notifications"].c.attempts

    assert attempts.server_default is not None
    assert "0" in str(attempts.server_default.arg)
    assert isinstance(attempts.type, Integer)
    assert isinstance(Base.metadata.tables["notifications"].c.order_id.type, Text)
    assert isinstance(Base.metadata.tables["notifications"].c.id.type, Uuid)


def test_the_logical_notification_is_unique_per_order_and_type() -> None:
    table = Base.metadata.tables["notifications"]
    uniques = [constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint)]

    assert len(uniques) == 1
    assert [column.name for column in uniques[0].columns] == ["order_id", "notification_type"]


def test_the_two_check_constraints_match_the_contract() -> None:
    table = Base.metadata.tables["notifications"]
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert len(checks) == 2
    type_check = checks["ck_notifications_notification_type"].lower()
    status_check = checks["ck_notifications_status"].lower()
    assert "notification_type" in type_check
    assert "'confirmation'" in type_check and "'cancellation'" in type_check
    assert "status" in status_check
    assert "'sent'" in status_check and "'failed'" in status_check


def test_the_primary_keys_are_the_contracted_ones() -> None:
    notifications = Base.metadata.tables["notifications"]
    processed = Base.metadata.tables["processed_events"]

    assert list(notifications.primary_key.columns.keys()) == ["id"]
    assert list(processed.primary_key.columns.keys()) == ["event_id"]


def test_the_rows_map_onto_the_tables() -> None:
    assert NotificationRow.__table__ is Base.metadata.tables["notifications"]
    assert ProcessedEventRow.__table__ is Base.metadata.tables["processed_events"]
