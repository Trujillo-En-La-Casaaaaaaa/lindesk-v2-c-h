"""Shared fixtures of all four suites of shopflow-notifications.

The unit and contract suites exercise the real application (real routers, real error
envelopes, real handlers, real templates) against in-process doubles of the two
ports, so they need no database, no broker and no provider.

The integration and failure suites wire the same application to the real adapters.
Their dependency fixtures live here too (they are lazy: a suite that never asks for
``engine`` or ``broker`` never touches the infrastructure), while the *isolation*
fixture of each suite stays in its own ``conftest.py``.

Dependency resolution: ``DATABASE_URL`` / ``BROKER_URL`` / ``PROVIDER_BASE_URL`` are
authoritative - the service reads exactly those variables - and each falls back to
the host-published port of the same dependency::

    docker compose -f ../shopflow-infra/docker-compose.yml up -d \
        notifications-db message-broker notification-provider-emulator

If no candidate answers, the suite fails with that command; it never silently skips.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.consumer.router import EventRouter
from app.db.session import create_engine_from_settings, create_session_factory
from app.handlers.orchestrator import NotificationOrchestrator
from app.main import create_app
from tests.support.broker import BrokerHarness
from tests.support.capture_provider import CaptureProvider
from tests.support.emulator import Emulator
from tests.support.fakes import FakeNotificationRepository, RecordingProvider
from tests.support.migrations import run_alembic
from tests.support.settings import (
    DEFAULT_TEST_BROKER_URL,
    DEFAULT_TEST_DATABASE_URL,
    DEFAULT_TEST_PROVIDER_BASE_URL,
    make_settings,
)

START_HINT = (
    "start the dependencies with\n"
    "  docker compose -f ../shopflow-infra/docker-compose.yml up -d "
    "notifications-db message-broker notification-provider-emulator"
)


# --------------------------------------------------------------------------- #
# in-process doubles (unit and contract suites)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repository() -> FakeNotificationRepository:
    """A store double with the real uniqueness and deduplication rules."""

    return FakeNotificationRepository()


@pytest.fixture()
def provider() -> RecordingProvider:
    """A provider double that records what the handlers deliver."""

    return RecordingProvider()


@pytest.fixture()
def orchestrator(repository: FakeNotificationRepository, provider: RecordingProvider) -> NotificationOrchestrator:
    return NotificationOrchestrator(repository=repository, provider=provider)


@pytest.fixture()
def router(orchestrator: NotificationOrchestrator) -> EventRouter:
    return EventRouter.from_orchestrator(orchestrator)


@pytest.fixture()
def app(repository: FakeNotificationRepository, provider: RecordingProvider):
    """The HTTP application with both ports backed by the doubles."""

    return create_app(repository=repository, provider=provider, start_consumer=False)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def capture_provider() -> Iterator[CaptureProvider]:
    """A real HTTP provider double (contracts.md §7) that records the bodies it receives."""

    with CaptureProvider() as provider:
        yield provider


# --------------------------------------------------------------------------- #
# dependency resolution (integration and failure suites)
# --------------------------------------------------------------------------- #
def _candidates(env_name: str, default: str) -> list[str]:
    return list(dict.fromkeys([url for url in (os.environ.get(env_name), default) if url]))


def _store_answers(url: str) -> None:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def _broker_answers(url: str) -> None:
    parameters = pika.URLParameters(url)
    if not parameters.virtual_host:
        parameters.virtual_host = "/"
    parameters.connection_attempts = 1
    parameters.socket_timeout = 5
    connection = pika.BlockingConnection(parameters)
    connection.close()


def _provider_answers(url: str) -> None:
    with httpx.Client(timeout=5.0) as client:
        response = client.get(f"{url.rstrip('/')}/ready")
    response.raise_for_status()


def _resolve(env_name: str, default: str, probe: Callable[[str], None], label: str) -> str:
    attempts: list[str] = []
    for candidate in _candidates(env_name, default):
        try:
            probe(candidate)
        except Exception as exc:  # noqa: BLE001 - any failure means "not this one"
            attempts.append(f"{candidate} ({type(exc).__name__}: {exc})")
            continue
        return candidate
    raise RuntimeError(f"the {label} is not reachable; {START_HINT}\ntried: {'; '.join(attempts) or 'no candidate'}")


@pytest.fixture(scope="session")
def store_url() -> str:
    return _resolve("DATABASE_URL", DEFAULT_TEST_DATABASE_URL, _store_answers, "notifications store")


@pytest.fixture(scope="session")
def broker_url() -> str:
    return _resolve("BROKER_URL", DEFAULT_TEST_BROKER_URL, _broker_answers, "broker")


@pytest.fixture(scope="session")
def provider_base_url() -> str:
    return _resolve("PROVIDER_BASE_URL", DEFAULT_TEST_PROVIDER_BASE_URL, _provider_answers, "provider emulator")


@pytest.fixture(scope="session")
def migrated_store(store_url: str) -> str:
    """Migrate the store to head once per session, exactly like shopflow-infra does."""

    completed = run_alembic(store_url, "upgrade", "head")
    if completed.returncode != 0:
        pytest.fail(
            f"the integration store could not be migrated; {START_HINT}\n"
            f"DATABASE_URL={store_url}\n{completed.stdout}\n{completed.stderr}"
        )
    return store_url


@pytest.fixture(scope="session")
def settings(migrated_store: str, broker_url: str, provider_base_url: str) -> Settings:
    """Settings pointed at the real dependencies with small retry bounds."""

    return make_settings(
        database_url=migrated_store,
        broker_url=broker_url,
        provider_base_url=provider_base_url,
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_from_settings(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture(scope="session")
def broker(settings: Settings) -> BrokerHarness:
    return BrokerHarness(settings.broker_url, queue=settings.broker_queue)


@pytest.fixture(scope="session")
def emulator(provider_base_url: str) -> Emulator:
    return Emulator(provider_base_url)


@pytest.fixture()
def case_id() -> str:
    """A unique seed, so repeated runs never share an order id or an event id."""

    return uuid.uuid4().hex[:12]


@pytest.fixture()
def client_factory(settings: Settings, engine: Engine):
    """Build a real application with test-specific settings (dead provider, ...)."""

    base = {
        "database_url": settings.database_url,
        "broker_url": settings.broker_url,
        "provider_base_url": settings.provider_base_url,
    }

    @contextmanager
    def factory(*, start_consumer: bool = True, provider: Any = None, **overrides: Any) -> Iterator[TestClient]:
        built = make_settings(**{**base, **overrides})
        app = create_app(settings=built, engine=engine, provider=provider, start_consumer=start_consumer)
        with TestClient(app) as test_client:
            yield test_client

    return factory
