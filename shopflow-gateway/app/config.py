"""Gateway configuration.

Every value is read from the environment (contracts.md §11); the gateway keeps
no secrets and stores nothing. There is deliberately no database setting: the
gateway owns no durable data and must not connect to any store.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ORDERS_BASE_URL = "http://shopflow-orders:8000"
DEFAULT_INVENTORY_BASE_URL = "http://shopflow-inventory:8000"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_GET_RETRY_ATTEMPTS = 1
MAX_GET_RETRY_ATTEMPTS = 2


class Settings(BaseSettings):
    """Runtime settings of the gateway.

    ``ORDERS_BASE_URL``, ``INVENTORY_BASE_URL`` and ``REQUEST_TIMEOUT_SECONDS``
    come from contracts.md §11. ``GET_RETRY_ATTEMPTS`` tunes the bounded retry
    of idempotent GET calls (default 1, hard capped at 2); state-changing POSTs
    are never retried.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    orders_base_url: str = Field(default=DEFAULT_ORDERS_BASE_URL)
    inventory_base_url: str = Field(default=DEFAULT_INVENTORY_BASE_URL)
    request_timeout_seconds: float = Field(default=DEFAULT_REQUEST_TIMEOUT_SECONDS, gt=0)
    get_retry_attempts: int = Field(default=DEFAULT_GET_RETRY_ATTEMPTS, ge=0, le=MAX_GET_RETRY_ATTEMPTS)

    @field_validator("orders_base_url", "inventory_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("base url must not be empty")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"base url must be an absolute http(s) url, got {value!r}")
        return candidate.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings, read once from the environment."""

    return Settings()
