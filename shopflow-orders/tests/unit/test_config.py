"""Unit tests of the configuration (contracts.md §11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import (
    BROKER_EXCHANGE,
    DEFAULT_BROKER_URL,
    DEFAULT_DATABASE_URL,
    DEFAULT_INVENTORY_BASE_URL,
    DEFAULT_PORT,
    DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ROUTING_KEY_ORDER_CANCELLED,
    ROUTING_KEY_ORDER_CONFIRMED,
    Settings,
    load_settings,
)


def test_documented_defaults_are_the_contracts_values() -> None:
    settings = Settings(_env_file=None)
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.broker_url == DEFAULT_BROKER_URL
    assert settings.inventory_base_url == DEFAULT_INVENTORY_BASE_URL
    assert settings.request_timeout_seconds == DEFAULT_REQUEST_TIMEOUT_SECONDS == 5
    assert settings.release_retry_interval_seconds == DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS == 5
    assert settings.port == DEFAULT_PORT == 8000
    assert settings.broker_exchange == BROKER_EXCHANGE == "shopflow.orders"
    assert settings.routing_keys == ("order.confirmed", "order.cancelled")
    assert settings.routing_keys == (ROUTING_KEY_ORDER_CONFIRMED, ROUTING_KEY_ORDER_CANCELLED)


def test_environment_variables_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@orders-db:5432/orders")
    monkeypatch.setenv("BROKER_URL", "amqp://guest:guest@broker:5672/")
    monkeypatch.setenv("INVENTORY_BASE_URL", "http://inventory:9000/")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("RELEASE_RETRY_INTERVAL_SECONDS", "1.5")
    monkeypatch.setenv("PORT", "8100")
    monkeypatch.setenv("START_WORKERS", "false")

    settings = load_settings()
    assert settings.database_url == "postgresql://user:pw@orders-db:5432/orders"
    assert settings.broker_url == "amqp://guest:guest@broker:5672/"
    assert settings.inventory_base_url == "http://inventory:9000"  # trailing slash trimmed
    assert settings.request_timeout_seconds == 2.5
    assert settings.release_retry_interval_seconds == 1.5
    assert settings.port == 8100
    assert settings.start_workers is False


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DATABASE_URL", "mysql://user:pw@host/db"),
        ("DATABASE_URL", "not-a-url"),
        ("BROKER_URL", "redis://localhost:6379/0"),
        ("INVENTORY_BASE_URL", "inventory:8000"),
        ("REQUEST_TIMEOUT_SECONDS", "0"),
        ("RELEASE_RETRY_INTERVAL_SECONDS", "-1"),
        ("PORT", "70000"),
    ],
)
def test_invalid_configuration_is_refused(monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(PydanticValidationError):
        load_settings()


def test_no_secret_is_stored_in_the_repository_defaults() -> None:
    settings = Settings(_env_file=None)
    assert "password" not in settings.database_url
    assert settings.request_timeout_seconds > 0
    assert settings.outbox_batch_size >= 1
    assert settings.reconciler_batch_size >= 1
    assert settings.reservation_grace_seconds >= 0
