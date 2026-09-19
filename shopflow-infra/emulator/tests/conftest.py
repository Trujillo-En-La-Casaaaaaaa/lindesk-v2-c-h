"""Test bootstrap for the provider emulator.

Makes the ``app`` package importable whichever way pytest is invoked — from the
repository root (``python -m pytest emulator/tests -q``, the way documented in
the handoff) or from inside ``emulator/`` (``python -m pytest tests -q``).
"""

from __future__ import annotations

import sys
from pathlib import Path

EMULATOR_ROOT = Path(__file__).resolve().parents[1]
if str(EMULATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(EMULATOR_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    """An in-process ASGI client (no sockets, no server)."""

    return TestClient(app)


@pytest.fixture(autouse=True)
def empty_store():
    """Every test starts and ends with an empty emulator."""

    store.reset()
    yield
    store.reset()
