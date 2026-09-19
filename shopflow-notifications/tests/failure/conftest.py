"""Fixtures of the failure suite.

The failure suite runs against the same real dependencies as the integration suite
(store, broker, emulator) and adds what the acceptance criteria require evidence
for: duplicate delivery, a provider outage with bounded retries and dead-lettering,
and a **real process crash** inside the two crash windows of the handoff.

Crashing on purpose needs a real OS process, so those tests start
``python -m app.consumer.consumer`` as a child with the documented fault-injection
switches. Everything else stays in-process for speed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.postgres_repository import PostgresNotificationRepository
from app.adapters.provider_http_client import HttpNotificationProvider
from app.config import Settings
from app.consumer.consumer import (
    CRASH_AFTER_COMMIT_ENV,
    CRASH_AFTER_PROVIDER_SEND_ENV,
    FaultHooks,
    OrderEventConsumer,
)
from app.consumer.router import EventRouter
from app.handlers.orchestrator import NotificationOrchestrator
from app.ports.provider import NotificationProvider
from app.ports.repository import NotificationRepository
from tests.support.broker import BrokerHarness
from tests.support.db import truncate_store
from tests.support.migrations import REPO_ROOT
from tests.support.runtimes import ConsumerThread

CRASH_EXIT_CODE = 9
CHILD_TIMEOUT_SECONDS = 120.0


@pytest.fixture(autouse=True)
def clean_state(session_factory: sessionmaker[Session], broker: BrokerHarness) -> Iterator[None]:
    """Every test starts with an empty store and empty queues (same as integration)."""

    truncate_store(session_factory)
    broker.purge()
    yield
    broker.purge()


@dataclass(frozen=True)
class Wiring:
    """The consumer under test plus the two adapters it uses."""

    consumer: OrderEventConsumer
    repository: NotificationRepository
    provider: NotificationProvider


def build_wiring(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    provider: NotificationProvider | None = None,
    hooks: FaultHooks | None = None,
) -> Wiring:
    """Assemble the real consumer (real store adapter, real provider adapter)."""

    repository = PostgresNotificationRepository(session_factory)
    if provider is None:
        provider = HttpNotificationProvider(
            settings.provider_base_url,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.provider_max_attempts,
            backoff_seconds=settings.provider_backoff_seconds,
            backoff_max_seconds=settings.provider_backoff_max_seconds,
        )
    orchestrator = NotificationOrchestrator(
        repository=repository,
        provider=provider,
        recipient_domain=settings.recipient_domain,
        after_send=(hooks.after_send if hooks else None),
    )
    consumer = OrderEventConsumer(
        settings=settings,
        router=EventRouter.from_orchestrator(orchestrator),
        hooks=hooks,
    )
    return Wiring(consumer=consumer, repository=repository, provider=provider)


@pytest.fixture()
def wiring_factory(settings: Settings, session_factory: sessionmaker[Session]):
    """Build a wiring on demand (a specific provider, fault hooks)."""

    def factory(
        *,
        provider: NotificationProvider | None = None,
        hooks: FaultHooks | None = None,
    ) -> Wiring:
        return build_wiring(settings, session_factory, provider=provider, hooks=hooks)

    return factory


@contextmanager
def running_consumer(wiring: Wiring) -> Iterator[Wiring]:
    """Run the consumer in a background thread for the duration of the block."""

    with ConsumerThread(wiring.consumer):
        yield wiring


def child_environment(settings: Settings, **extra: str) -> dict[str, str]:
    """The environment of a consumer child process (documented variables only)."""

    environment = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "BROKER_URL": settings.broker_url,
        "PROVIDER_BASE_URL": settings.provider_base_url,
        "REQUEST_TIMEOUT_SECONDS": str(settings.request_timeout_seconds),
        "CONSUMER_PREFETCH": str(settings.consumer_prefetch),
        "CONSUMER_MAX_DELIVERY_ATTEMPTS": str(settings.consumer_max_delivery_attempts),
        "CONSUMER_RETRY_BACKOFF_SECONDS": str(settings.consumer_retry_backoff_seconds),
        "CONSUMER_RETRY_BACKOFF_MAX_SECONDS": str(settings.consumer_retry_backoff_max_seconds),
        "CONSUMER_RECONNECT_INTERVAL_SECONDS": str(settings.consumer_reconnect_interval_seconds),
        "PROVIDER_MAX_ATTEMPTS": str(settings.provider_max_attempts),
        "PROVIDER_BACKOFF_SECONDS": str(settings.provider_backoff_seconds),
        "PROVIDER_BACKOFF_MAX_SECONDS": str(settings.provider_backoff_max_seconds),
        "LOG_LEVEL": "INFO",
    }
    environment.pop(CRASH_AFTER_PROVIDER_SEND_ENV, None)
    environment.pop(CRASH_AFTER_COMMIT_ENV, None)
    environment.update(extra)
    return environment


class ConsumerProcess:
    """A ``python -m app.consumer.consumer`` child process under test control."""

    def __init__(self, process: subprocess.Popen[bytes], log_path: Path) -> None:
        self.process = process
        self.log_path = log_path

    def wait(self, timeout: float = CHILD_TIMEOUT_SECONDS) -> int:
        return int(self.process.wait(timeout=timeout))

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - only on a stuck child
                self.process.kill()
                self.process.wait(timeout=30)

    @property
    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""


@pytest.fixture()
def consumer_process(tmp_path: Path, settings: Settings):
    """Start the consumer as a child process; always stop it at the end of the test."""

    started: list[ConsumerProcess] = []

    def start(**extra: str) -> ConsumerProcess:
        log_path = tmp_path / f"consumer-{len(started) + 1}.log"
        handle = log_path.open("wb")
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter, fixed module
            [sys.executable, "-m", "app.consumer.consumer"],
            cwd=REPO_ROOT,
            env=child_environment(settings, **extra),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        child = ConsumerProcess(process, log_path)
        started.append(child)
        return child

    yield start

    for child in started:
        child.terminate()
