"""Fixtures of the contract suite.

The contract suite runs without infrastructure: it exercises the real routers, the
real templates and the real error envelopes against in-process doubles of the two
ports.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db.session import database_is_reachable
from app.main import create_app
from tests.support.fakes import (
    FakeNotificationRepository,
    HealthyEngine,
    RecordingProbe,
    RecordingProvider,
    UnreachableEngine,
)


def _client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def ready_client(
    repository: FakeNotificationRepository,
    provider: RecordingProvider,
) -> Iterator[TestClient]:
    """A client whose store, broker and provider all answer the readiness probe."""

    app = create_app(
        engine=HealthyEngine(),
        repository=repository,
        provider=provider,
        readiness={
            "store": RecordingProbe(reachable=True),
            "broker": RecordingProbe(reachable=True),
            "provider": RecordingProbe(reachable=True),
        },
        start_consumer=False,
    )
    yield from _client(app)


@pytest.fixture()
def unready_client(
    repository: FakeNotificationRepository,
    provider: RecordingProvider,
) -> Iterator[TestClient]:
    """A client whose store refuses to answer the readiness probe."""

    app = create_app(
        engine=UnreachableEngine(),
        repository=repository,
        provider=provider,
        readiness={
            # the real probe implementation, against an engine that refuses
            "store": lambda: database_is_reachable(UnreachableEngine()),
            "broker": RecordingProbe(reachable=True),
            "provider": RecordingProbe(reachable=True),
        },
        start_consumer=False,
    )
    yield from _client(app)
