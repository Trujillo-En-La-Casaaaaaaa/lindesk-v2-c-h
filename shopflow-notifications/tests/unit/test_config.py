"""Unit tests of the configuration (contracts.md §11, handoff constraints).

The service reads configuration from the environment only, keeps no credential and
validates every bound that limits retrying, so a misconfiguration fails loudly at
startup instead of looping at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    DEAD_LETTER_QUEUE_NAME,
    DEFAULT_DATABASE_URL,
    DEFAULT_PROVIDER_BASE_URL,
    DEFAULT_RECIPIENT_DOMAIN,
    EXCHANGE_NAME,
    QUEUE_NAME,
    RETRY_QUEUE_NAME,
    ROUTING_KEY_CANCELLED,
    ROUTING_KEY_CONFIRMED,
    Settings,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The variables contracts.md §11 assigns to this service.
CONTRACT_VARIABLES = ("DATABASE_URL", "BROKER_URL", "PROVIDER_BASE_URL", "REQUEST_TIMEOUT_SECONDS")


def test_defaults_are_the_documented_contract_values() -> None:
    settings = Settings()

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert "/shopflow_notifications" in settings.database_url
    assert settings.broker_url == "amqp://shopflow:shopflow@message-broker:5672/"
    assert settings.provider_base_url == DEFAULT_PROVIDER_BASE_URL
    assert settings.request_timeout_seconds == 5
    assert settings.port == 8000


def test_defaults_match_the_contract_topology() -> None:
    settings = Settings()

    assert settings.broker_exchange == EXCHANGE_NAME == "shopflow.orders"
    assert settings.broker_queue == QUEUE_NAME == "shopflow.notifications.order-events"
    assert settings.routing_keys == (ROUTING_KEY_CONFIRMED, ROUTING_KEY_CANCELLED)
    assert settings.broker_retry_queue == RETRY_QUEUE_NAME
    assert settings.broker_dead_letter_queue == DEAD_LETTER_QUEUE_NAME


def test_environment_overrides_every_contract_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5435/shopflow_notifications")
    monkeypatch.setenv("BROKER_URL", "amqp://guest:guest@localhost:5672/")
    monkeypatch.setenv("PROVIDER_BASE_URL", "http://localhost:8081/")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "2.5")

    settings = load_settings()

    assert settings.database_url == "postgresql://user:pw@localhost:5435/shopflow_notifications"
    assert settings.broker_url == "amqp://guest:guest@localhost:5672/"
    assert settings.provider_base_url == "http://localhost:8081"  # trailing slash trimmed
    assert settings.request_timeout_seconds == 2.5


def test_prefetch_stays_inside_the_contract_range() -> None:
    assert Settings(consumer_prefetch=1).consumer_prefetch == 1
    assert Settings(consumer_prefetch=10).consumer_prefetch == 10
    with pytest.raises(ValidationError):
        Settings(consumer_prefetch=0)
    with pytest.raises(ValidationError):
        Settings(consumer_prefetch=11)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "mysql://user:pw@db:3306/shopflow"),
        ("broker_url", "redis://localhost:6379/0"),
        ("provider_base_url", "ftp://provider:21"),
        ("recipient_domain", ""),
        ("recipient_domain", "not@a/domain"),
        ("port", 0),
        ("request_timeout_seconds", 0),
        ("consumer_max_delivery_attempts", 0),
        ("provider_max_attempts", 0),
    ],
)
def test_invalid_configuration_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_backoff_ceilings_must_not_be_below_their_base() -> None:
    with pytest.raises(ValidationError):
        Settings(consumer_retry_backoff_seconds=2, consumer_retry_backoff_max_seconds=1)
    with pytest.raises(ValidationError):
        Settings(provider_backoff_seconds=2, provider_backoff_max_seconds=1)


def test_routing_keys_are_parsed_and_validated() -> None:
    assert Settings(broker_routing_keys="order.confirmed, order.cancelled").routing_keys == (
        "order.confirmed",
        "order.cancelled",
    )
    with pytest.raises(ValidationError):
        Settings(broker_routing_keys=" , ")


def test_recipient_domain_has_a_deterministic_default() -> None:
    assert Settings().recipient_domain == DEFAULT_RECIPIENT_DOMAIN


def test_no_credential_is_stored_in_the_configuration() -> None:
    settings = Settings()

    assert set(settings.model_dump()) & {"password", "secret", "token", "api_key"} == set()
    # The documented local values are credential-free throwaways (contracts.md §11).
    assert "shopflow:shopflow@" in settings.database_url
    assert "shopflow:shopflow@" in settings.broker_url


def test_env_example_documents_every_contract_variable() -> None:
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for name in CONTRACT_VARIABLES:
        assert f"{name}=" in content, f".env.example does not document {name}"
    assert "DATABASE_URL=postgresql://shopflow:shopflow@localhost:5435/shopflow_notifications" in content
    assert "PROVIDER_BASE_URL=http://localhost:8081" in content
