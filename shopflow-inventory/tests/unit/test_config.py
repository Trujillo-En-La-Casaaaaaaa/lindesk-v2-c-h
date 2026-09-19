"""Unit tests: configuration is read from the documented environment variables.

The suite clears the documented variables first, so the result does not depend on
the shell that runs it (a CI or compose environment may set ``DATABASE_URL``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_DATABASE_URL, DEFAULT_PORT, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTED_VARIABLES = (
    "DATABASE_URL",
    "PORT",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
    "DB_POOL_RECYCLE_SECONDS",
)


@pytest.fixture()
def documented_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DOCUMENTED_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_documented_defaults(documented_environment: None) -> None:
    settings = Settings()

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.port == DEFAULT_PORT


def test_environment_overrides_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/shopflow_inventory")
    monkeypatch.setenv("PORT", "9001")

    settings = Settings()

    assert settings.database_url == "postgresql://user:pw@localhost:5432/shopflow_inventory"
    assert settings.port == 9001


def test_no_credentials_are_stored_in_the_repository() -> None:
    env_template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DATABASE_URL=" in env_template
    assert "PORT=" in env_template
    # the local defaults are the throwaway values of contracts.md §11
    assert "change-me" not in env_template.lower()


@pytest.mark.parametrize("value", ["sqlite:///inventory.db", "mysql://user:pw@host/db", ""])
def test_a_non_postgresql_store_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=value)


def test_only_the_documented_variables_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown environment variables are ignored, never interpreted as settings."""

    monkeypatch.setenv("SHOPFLOW_UNRELATED", "ignored")
    monkeypatch.setenv("PROVIDER_BASE_URL", "http://notification-provider-emulator:8000")

    settings = Settings()

    assert os.environ["SHOPFLOW_UNRELATED"] == "ignored"
    assert not hasattr(settings, "provider_base_url")
