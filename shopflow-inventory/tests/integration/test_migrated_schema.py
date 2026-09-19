"""Integration tests: the migrated schema really enforces the two invariants."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from tests.integration.conftest import alembic

OWNED_TABLES = {"products", "stock", "stock_movements"}
MANAGED_TABLES = OWNED_TABLES | {"alembic_version"}


def test_the_migration_is_at_head_and_idempotent() -> None:
    current = alembic("current")
    assert current.returncode == 0, current.stderr
    assert "0001_inventory_schema" in current.stdout

    upgrade = alembic("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr


def test_only_the_owned_tables_exist(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == MANAGED_TABLES


def test_the_schema_matches_the_orm_models(engine: Engine) -> None:
    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        assert table_name in inspector.get_table_names()
        stored_columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert stored_columns == {column.name for column in table.columns}, table_name


def test_stock_has_the_never_negative_check_constraint(engine: Engine) -> None:
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'ck_stock_quantity_non_negative'"
            )
        ).scalar()

    assert definition is not None, "the never-negative CHECK constraint must exist in the store"
    assert "quantity >= 0" in definition


def test_stock_has_the_movement_uniqueness_constraint(engine: Engine) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname IN ('uq_stock_movements_order_operation', 'ck_stock_movements_operation')"
            )
        ).all()

    definitions = {str(row[1]) for row in rows}
    assert any("UNIQUE (order_id, operation)" in definition for definition in definitions)
    assert any(
        "operation" in definition and "reserve" in definition and "release" in definition
        for definition in definitions
    )


def test_the_store_refuses_a_negative_quantity(engine: Engine) -> None:
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE stock SET quantity = -1 WHERE product_id = 'p-1001'"))


def test_the_store_refuses_a_duplicate_movement(engine: Engine) -> None:
    insert = text(
        "INSERT INTO stock_movements (id, order_id, operation, payload, created_at)"
        " VALUES (:id, 'o-dup', 'reserve', '{\"lines\": []}'::jsonb, now())"
    )
    with engine.begin() as connection:
        connection.execute(insert, {"id": str(uuid.uuid4())})
        with pytest.raises(IntegrityError):
            connection.execute(insert, {"id": str(uuid.uuid4())})


def test_the_store_refuses_an_unknown_operation(engine: Engine) -> None:
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO stock_movements (id, order_id, operation, payload, created_at)"
                " VALUES (:id, 'o-bad-op', 'cancel', '{}'::jsonb, now())"
            ),
            {"id": str(uuid.uuid4())},
        )


def test_stock_references_the_catalogue(engine: Engine) -> None:
    foreign_keys = inspect(engine).get_foreign_keys("stock")

    assert [(fk["referred_table"], fk["constrained_columns"]) for fk in foreign_keys] == [
        ("products", ["product_id"])
    ]


def test_the_seeded_store_holds_the_documented_levels(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        rows = dict(session.execute(text("SELECT product_id, quantity FROM stock")).all())

    assert rows == {"p-1001": 50, "p-1002": 25, "p-1003": 10, "p-1004": 100, "p-1005": 0}
