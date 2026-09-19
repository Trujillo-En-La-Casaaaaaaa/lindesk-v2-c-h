"""Configuration comes from the environment only (contracts.md §11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ORDERS_BASE_URL", "INVENTORY_BASE_URL", "REQUEST_TIMEOUT_SECONDS", "GET_RETRY_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_defaults_match_the_contract() -> None:
    settings = Settings()

    assert settings.orders_base_url == "http://shopflow-orders:8000"
    assert settings.inventory_base_url == "http://shopflow-inventory:8000"
    assert settings.request_timeout_seconds == 5.0
    assert settings.get_retry_attempts == 1


def test_environment_overrides_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_BASE_URL", "http://orders.internal:9000/")
    monkeypatch.setenv("INVENTORY_BASE_URL", "http://inventory.internal:9001")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("GET_RETRY_ATTEMPTS", "2")

    settings = Settings()

    assert settings.orders_base_url == "http://orders.internal:9000"
    assert settings.inventory_base_url == "http://inventory.internal:9001"
    assert settings.request_timeout_seconds == 0.25
    assert settings.get_retry_attempts == 2


def test_get_settings_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVENTORY_BASE_URL", "http://inventory.example:1234")
    get_settings.cache_clear()

    assert get_settings().inventory_base_url == "http://inventory.example:1234"

    get_settings.cache_clear()


@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "not-a-number"])
def test_invalid_timeout_is_rejected(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["-1", "3", "99"])
def test_retry_attempts_are_bounded(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GET_RETRY_ATTEMPTS", value)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["", "   ", "shopflow-orders:8000", "ftp://orders:8000", "/orders"])
def test_invalid_base_urls_are_rejected(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_BASE_URL", value)

    with pytest.raises(ValidationError):
        Settings()


def test_gateway_has_no_database_configuration() -> None:
    keys = {key.upper() for key in Settings().model_dump()}

    assert not [key for key in keys if "DATABASE" in key or "BROKER" in key or "REDIS" in key]
