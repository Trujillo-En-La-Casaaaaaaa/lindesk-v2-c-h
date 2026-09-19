"""Engine and session factory of the owned PostgreSQL store.

The connection string comes from ``DATABASE_URL`` (contracts.md §11); nothing is
stored in code. ``/ready`` uses :func:`database_is_reachable` to answer ``200``
only when the store actually answers.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

READINESS_PROBE_SQL = text("SELECT 1")


def create_engine_from_settings(settings: Settings) -> Engine:
    """Create the SQLAlchemy engine for the owned inventory database."""

    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the session factory used by the adapters."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def database_is_reachable(engine: Engine) -> bool:
    """Return ``True`` when the store answers a trivial query."""

    try:
        with engine.connect() as connection:
            connection.execute(READINESS_PROBE_SQL)
    except SQLAlchemyError:
        return False
    return True
