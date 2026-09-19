"""Fixtures of the integration suite (real PostgreSQL, real HTTP server)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import StockMovementRow
from app.db.session import create_engine_from_settings, create_session_factory
from app.main import create_app
from app.seed import seed_inventory
from tests.support.servers import RunningServer, start_server

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The inventory database published by shopflow-infra's compose file on the host.
DEFAULT_DATABASE_URL = "postgresql://shopflow:shopflow@localhost:5434/shopflow_inventory"


def candidate_database_urls() -> list[str]:
    """The stores to try, in order: ``DATABASE_URL`` first, then the published port.

    ``DATABASE_URL`` is authoritative (the service reads exactly that variable),
    but inside a compose network it names a host only reachable from other
    containers (``inventory-db:5432``). The host-published port of the same store
    is therefore the fallback, so the suite runs from the host either way.
    """

    candidates = [url for url in (os.environ.get("DATABASE_URL"), DEFAULT_DATABASE_URL) if url]
    return list(dict.fromkeys(candidates))


def resolve_database_url() -> str:
    """Return the first candidate the store actually answers on."""

    attempts: list[str] = []
    for candidate in candidate_database_urls():
        try:
            engine = create_engine(candidate, pool_pre_ping=True)
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
        except SQLAlchemyError as exc:
            attempts.append(f"{candidate} ({type(exc).__name__})")
            continue
        return candidate
    raise RuntimeError(
        "the inventory store is not reachable; start it with\n"
        "  docker compose -f ../shopflow-infra/docker-compose.yml up -d inventory-db\n"
        f"tried: {', '.join(attempts) or 'no candidate'}"
    )


def database_url() -> str:
    """The store under test (``DATABASE_URL`` wins when it is reachable)."""

    if "url" not in _RESOLVED:
        _RESOLVED["url"] = resolve_database_url()
    return _RESOLVED["url"]


_RESOLVED: dict[str, str] = {}


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the repository's own migration tool against the store under test."""

    return subprocess.run(  # noqa: S603 - fixed interpreter and fixed alembic arguments
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url()},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def store_url() -> str:
    """Migrate the store to head once per session, exactly like shopflow-infra."""

    url = database_url()
    completed = alembic("upgrade", "head")
    if completed.returncode != 0:
        pytest.fail(
            "the integration store is not reachable or the migration failed; start it with\n"
            f"  docker compose -f ../shopflow-infra/docker-compose.yml up -d inventory-db\n"
            f"DATABASE_URL={url}\n{completed.stdout}\n{completed.stderr}"
        )
    return url


@pytest.fixture(scope="session")
def settings(store_url: str) -> Settings:
    return Settings(database_url=store_url)


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


@pytest.fixture(autouse=True)
def seeded_store(session_factory: sessionmaker[Session]) -> Iterator[None]:
    """Give every test the documented catalogue with no recorded movements."""

    with session_factory() as session, session.begin():
        session.execute(delete(StockMovementRow))
    seed_inventory(session_factory)
    yield


@pytest.fixture()
def app(engine: Engine):
    """The service with both ports wired to the real PostgreSQL store."""

    return create_app(engine=engine)


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def restarted_client(settings: Settings) -> Iterator[TestClient]:
    """A client of a *new* process-like instance (new engine, new session factory).

    Used to prove that idempotency and stock state live in the store: a retry
    after a restart must return the same logical outcome.
    """

    restarted_engine = create_engine_from_settings(settings)
    restarted_app = create_app(engine=restarted_engine)
    try:
        with TestClient(restarted_app) as test_client:
            yield test_client
    finally:
        restarted_engine.dispose()


@pytest.fixture(scope="session")
def server(engine: Engine) -> Iterator[RunningServer]:
    """The real ASGI application on a real socket, for concurrent HTTP tests."""

    running_app = create_app(engine=engine)
    with start_server(running_app) as running_server:
        yield running_server
