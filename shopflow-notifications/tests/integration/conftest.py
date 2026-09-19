"""Fixtures of the integration suite.

The dependency fixtures (store, broker, emulator, settings, engine) live in the
top-level ``tests/conftest.py`` because the failure suite needs exactly the same
ones. This module adds the per-test isolation of the integration suite.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.support.broker import BrokerHarness
from tests.support.db import truncate_store


@pytest.fixture(autouse=True)
def clean_state(session_factory: sessionmaker[Session], broker: BrokerHarness) -> Iterator[None]:
    """Every test starts with an empty store and empty queues.

    The provider emulator is deliberately *not* reset: it is a shared, in-memory
    test double, and every test works on its own order ids, so a leftover message
    of another test can never be mistaken for a delivery of this one.
    """

    truncate_store(session_factory)
    broker.purge()
    yield
    broker.purge()
