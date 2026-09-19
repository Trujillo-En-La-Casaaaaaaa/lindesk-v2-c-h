"""Settings factory shared by the unit, integration and failure suites.

The defaults are the documented contracts.md §11 values pointed at the stores the
``shopflow-infra`` compose file publishes on the host, so a suite can build the
application exactly as the deployed one is built - only with small retry bounds so
a failure scenario finishes in milliseconds instead of seconds.
"""

from __future__ import annotations

from app.config import Settings

#: Host-published stores of the `shopflow` compose project (see docker-compose.yml:
#: notifications-db 5435, message-broker 5672, notification-provider-emulator 8081).
DEFAULT_TEST_DATABASE_URL = "postgresql://shopflow:shopflow@localhost:5435/shopflow_notifications"
DEFAULT_TEST_BROKER_URL = "amqp://shopflow:shopflow@localhost:5672/"
DEFAULT_TEST_PROVIDER_BASE_URL = "http://localhost:8081"

#: Small but real bounds: the retry policy is exercised, not short-circuited.
DEFAULT_TEST_OVERRIDES: dict[str, object] = {
    "request_timeout_seconds": 2.0,
    "consumer_prefetch": 5,
    "consumer_max_delivery_attempts": 2,
    "consumer_retry_backoff_seconds": 0.05,
    "consumer_retry_backoff_max_seconds": 0.2,
    "consumer_reconnect_interval_seconds": 0.2,
    "provider_max_attempts": 2,
    "provider_backoff_seconds": 0.01,
    "provider_backoff_max_seconds": 0.05,
}


def make_settings(**overrides: object) -> Settings:
    """Build settings on the host-published stores with fast retry bounds."""

    values: dict[str, object] = {
        "database_url": DEFAULT_TEST_DATABASE_URL,
        "broker_url": DEFAULT_TEST_BROKER_URL,
        "provider_base_url": DEFAULT_TEST_PROVIDER_BASE_URL,
        **DEFAULT_TEST_OVERRIDES,
    }
    values.update(overrides)
    # Keep the validated invariants satisfied when a test only moves a base delay.
    _widen_backoff(values, "consumer_retry_backoff_seconds", "consumer_retry_backoff_max_seconds")
    _widen_backoff(values, "provider_backoff_seconds", "provider_backoff_max_seconds")
    return Settings(**values)


def _widen_backoff(values: dict[str, object], base_key: str, max_key: str) -> None:
    base = float(values[base_key])  # type: ignore[arg-type]
    if float(values[max_key]) < base:  # type: ignore[arg-type]
        values[max_key] = base


__all__ = [
    "DEFAULT_TEST_BROKER_URL",
    "DEFAULT_TEST_DATABASE_URL",
    "DEFAULT_TEST_OVERRIDES",
    "DEFAULT_TEST_PROVIDER_BASE_URL",
    "make_settings",
]
