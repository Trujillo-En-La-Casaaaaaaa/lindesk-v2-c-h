"""Configuration of shopflow-notifications, read from the environment (contracts.md §11).

Only the documented variables are used and no credential is stored here: the
local defaults are the throwaway, credential-free values of contracts.md §11 for
local Docker.

The broker topology names of contracts.md §8 and the retry / dead-letter names
this service owns are configuration as well, so an operator can point the
service at a renamed topology without a code change. Every knob that bounds
retrying (``CONSUMER_MAX_DELIVERY_ATTEMPTS``, ``PROVIDER_MAX_ATTEMPTS`` and the
backoff ceilings) is explicit and validated: the service must never retry
forever and must never hot-loop.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: contracts.md §11 local default of the notifications store.
DEFAULT_DATABASE_URL = "postgresql://shopflow:shopflow@notifications-db:5432/shopflow_notifications"
#: contracts.md §11 local default of the broker.
DEFAULT_BROKER_URL = "amqp://shopflow:shopflow@message-broker:5672/"
#: contracts.md §11 local default of the provider emulator.
DEFAULT_PROVIDER_BASE_URL = "http://notification-provider-emulator:8000"
#: contracts.md §11 local default of ``REQUEST_TIMEOUT_SECONDS``.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_PORT = 8000

# --- broker topology of contracts.md §8 (consumer binding is fixed by contract) --
EXCHANGE_NAME = "shopflow.orders"
QUEUE_NAME = "shopflow.notifications.order-events"
ROUTING_KEY_CONFIRMED = "order.confirmed"
ROUTING_KEY_CANCELLED = "order.cancelled"
DEFAULT_ROUTING_KEYS = f"{ROUTING_KEY_CONFIRMED},{ROUTING_KEY_CANCELLED}"

# --- objects this service owns (retrying and dead-lettering, handoff #1) --------
RETRY_EXCHANGE_NAME = "shopflow.notifications.retry"
RETRY_QUEUE_NAME = "shopflow.notifications.order-events.retry"
DEAD_LETTER_EXCHANGE_NAME = "shopflow.notifications.dead-letter"
DEAD_LETTER_QUEUE_NAME = "shopflow.notifications.order-events.dead"

# --- bounded retry defaults -----------------------------------------------------
DEFAULT_CONSUMER_PREFETCH = 5
DEFAULT_CONSUMER_MAX_DELIVERY_ATTEMPTS = 3
DEFAULT_CONSUMER_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_CONSUMER_RETRY_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_CONSUMER_RECONNECT_INTERVAL_SECONDS = 5.0
DEFAULT_PROVIDER_MAX_ATTEMPTS = 3
DEFAULT_PROVIDER_BACKOFF_SECONDS = 0.5
DEFAULT_PROVIDER_BACKOFF_MAX_SECONDS = 5.0

#: Recipient address domain used to address a customer from ``customerId`` alone.
DEFAULT_RECIPIENT_DOMAIN = "notifications.shopflow.local"

_SUPPORTED_DATABASE_SCHEMES = ("postgresql://", "postgresql+psycopg2://")
_SUPPORTED_BROKER_SCHEMES = ("amqp://", "amqps://")
_SUPPORTED_PROVIDER_SCHEMES = ("http://", "https://")


class Settings(BaseSettings):
    """Runtime settings of the notifications service."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # contracts.md §11
    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    broker_url: str = Field(default=DEFAULT_BROKER_URL)
    provider_base_url: str = Field(default=DEFAULT_PROVIDER_BASE_URL)
    request_timeout_seconds: float = Field(default=DEFAULT_REQUEST_TIMEOUT_SECONDS, gt=0, le=120)
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)

    # connection pool of the owned store
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)

    # consumer (handoff #1)
    consumer_enabled: bool = Field(default=True)
    consumer_prefetch: int = Field(default=DEFAULT_CONSUMER_PREFETCH, ge=1, le=10)
    consumer_max_delivery_attempts: int = Field(default=DEFAULT_CONSUMER_MAX_DELIVERY_ATTEMPTS, ge=1, le=100)
    consumer_retry_backoff_seconds: float = Field(default=DEFAULT_CONSUMER_RETRY_BACKOFF_SECONDS, ge=0, le=600)
    consumer_retry_backoff_max_seconds: float = Field(default=DEFAULT_CONSUMER_RETRY_BACKOFF_MAX_SECONDS, ge=0, le=3600)
    consumer_reconnect_interval_seconds: float = Field(
        default=DEFAULT_CONSUMER_RECONNECT_INTERVAL_SECONDS,
        gt=0,
        le=600,
    )

    # provider adapter (handoff #5)
    provider_max_attempts: int = Field(default=DEFAULT_PROVIDER_MAX_ATTEMPTS, ge=1, le=20)
    provider_backoff_seconds: float = Field(default=DEFAULT_PROVIDER_BACKOFF_SECONDS, ge=0, le=60)
    provider_backoff_max_seconds: float = Field(default=DEFAULT_PROVIDER_BACKOFF_MAX_SECONDS, ge=0, le=600)

    # deterministic recipient addressing (no preferences store, handoff scope lock)
    recipient_domain: str = Field(default=DEFAULT_RECIPIENT_DOMAIN)

    # broker topology (contracts.md §8 names by default)
    broker_exchange: str = Field(default=EXCHANGE_NAME)
    broker_queue: str = Field(default=QUEUE_NAME)
    broker_routing_keys: str = Field(default=DEFAULT_ROUTING_KEYS)
    broker_retry_exchange: str = Field(default=RETRY_EXCHANGE_NAME)
    broker_retry_queue: str = Field(default=RETRY_QUEUE_NAME)
    broker_dead_letter_exchange: str = Field(default=DEAD_LETTER_EXCHANGE_NAME)
    broker_dead_letter_queue: str = Field(default=DEAD_LETTER_QUEUE_NAME)

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

    @field_validator("provider_base_url")
    @classmethod
    def _validate_provider_base_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        if not candidate.startswith(_SUPPORTED_PROVIDER_SCHEMES):
            raise ValueError("PROVIDER_BASE_URL must be an http:// url")
        return candidate

    @field_validator("recipient_domain")
    @classmethod
    def _validate_recipient_domain(cls, value: str) -> str:
        candidate = value.strip().strip("@")
        if not candidate or "@" in candidate:
            raise ValueError("NOTIFICATION_RECIPIENT_DOMAIN must be a bare domain name")
        return candidate

    @model_validator(mode="after")
    def _validate_retry_bounds(self) -> Settings:
        if self.consumer_retry_backoff_max_seconds < self.consumer_retry_backoff_seconds:
            raise ValueError(
                "CONSUMER_RETRY_BACKOFF_MAX_SECONDS must be >= CONSUMER_RETRY_BACKOFF_SECONDS"
            )
        if self.provider_backoff_max_seconds < self.provider_backoff_seconds:
            raise ValueError("PROVIDER_BACKOFF_MAX_SECONDS must be >= PROVIDER_BACKOFF_SECONDS")
        if not self.routing_keys:
            raise ValueError("BROKER_ROUTING_KEYS must list at least one routing key")
        return self

    @property
    def routing_keys(self) -> tuple[str, ...]:
        """The routing keys this consumer binds, in configuration order."""

        return tuple(key.strip() for key in self.broker_routing_keys.split(",") if key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings, read once from the environment."""

    return Settings()


def load_settings() -> Settings:
    """Return the process settings (uncached variant for tests and CLI entry points)."""

    return Settings()
