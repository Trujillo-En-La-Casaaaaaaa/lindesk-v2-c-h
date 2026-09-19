"""Configuration of shopflow-orders, read from the environment (contracts.md §11).

Only the documented variables are required; every optional knob has a local
default and is listed in ``.env.example``. No credential lives in this file: the
local defaults are the throwaway values of contracts.md §11 for local Docker.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: contracts.md §11 default of the owned orders store.
DEFAULT_DATABASE_URL = "postgresql://shopflow:shopflow@orders-db:5432/shopflow_orders"
#: contracts.md §11 default of the broker.
DEFAULT_BROKER_URL = "amqp://shopflow:shopflow@message-broker:5672/"
#: contracts.md §11 default of the inventory service.
DEFAULT_INVENTORY_BASE_URL = "http://shopflow-inventory:8000"
#: contracts.md §11 default of the outbound HTTP timeout (gateway, orders, notifications).
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
#: contracts.md §11 default of the release reconciler interval.
DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS = 5.0
#: The HTTP port of the service (shopflow-infra publishes it as 8001 -> 8000).
DEFAULT_PORT = 8000

#: contracts.md §8: the durable topic exchange this service publishes to.
BROKER_EXCHANGE = "shopflow.orders"
#: contracts.md §8: the two routing keys, which are also the event types.
ROUTING_KEY_ORDER_CONFIRMED = "order.confirmed"
ROUTING_KEY_ORDER_CANCELLED = "order.cancelled"

_SUPPORTED_DATABASE_SCHEMES = ("postgresql://", "postgresql+psycopg2://")
_SUPPORTED_BROKER_SCHEMES = ("amqp://", "amqps://")


class Settings(BaseSettings):
    """Runtime settings of the orders service."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # -- contracts.md §11 ---------------------------------------------------
    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    broker_url: str = Field(default=DEFAULT_BROKER_URL)
    inventory_base_url: str = Field(default=DEFAULT_INVENTORY_BASE_URL)
    request_timeout_seconds: float = Field(default=DEFAULT_REQUEST_TIMEOUT_SECONDS, gt=0)
    release_retry_interval_seconds: float = Field(default=DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS, gt=0)
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)

    # -- optional tuning (documented in .env.example, sane local defaults) ---
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)

    broker_exchange: str = Field(default=BROKER_EXCHANGE, min_length=1)
    broker_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, ge=1, le=1000)
    outbox_max_backoff_seconds: float = Field(default=30.0, gt=0)

    reconciler_batch_size: int = Field(default=50, ge=1, le=1000)
    #: A ``CREATED`` order is retried by the reconciler only after this grace
    #: period, so an in-flight create is never raced by the background relay.
    reservation_grace_seconds: float = Field(default=30.0, ge=0)

    #: The background relays run inside the service process (the single container
    #: shopflow-infra starts). Set to false for a worker-free runtime.
    start_workers: bool = Field(default=True)

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate.startswith(_SUPPORTED_DATABASE_SCHEMES):
            raise ValueError("DATABASE_URL must be a postgresql:// url")
        return candidate

    @field_validator("broker_url")
    @classmethod
    def _validate_broker_url(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate.startswith(_SUPPORTED_BROKER_SCHEMES):
            raise ValueError("BROKER_URL must be an amqp:// url")
        return candidate

    @field_validator("inventory_base_url")
    @classmethod
    def _validate_inventory_base_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("INVENTORY_BASE_URL must be an http(s):// url")
        return candidate

    @property
    def routing_keys(self) -> tuple[str, str]:
        """The two routing keys of contracts.md §8."""

        return (ROUTING_KEY_ORDER_CONFIRMED, ROUTING_KEY_ORDER_CANCELLED)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings, read once from the environment."""

    return Settings()


def load_settings() -> Settings:
    """Return the process settings (uncached variant for tests and CLI entry points)."""

    return Settings()


__all__ = [
    "BROKER_EXCHANGE",
    "DEFAULT_BROKER_URL",
    "DEFAULT_DATABASE_URL",
    "DEFAULT_INVENTORY_BASE_URL",
    "DEFAULT_PORT",
    "DEFAULT_RELEASE_RETRY_INTERVAL_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "ROUTING_KEY_ORDER_CANCELLED",
    "ROUTING_KEY_ORDER_CONFIRMED",
    "Settings",
    "get_settings",
    "load_settings",
]
