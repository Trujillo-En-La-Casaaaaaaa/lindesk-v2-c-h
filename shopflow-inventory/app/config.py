"""Configuration of shopflow-inventory, read from the environment (contracts.md §11).

Only the documented variables are used, and no credential is stored here: the
local defaults are the throwaway values of contracts.md §11 for local Docker.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: contracts.md §11 default of the inventory store.
DEFAULT_DATABASE_URL = "postgresql://shopflow:shopflow@inventory-db:5432/shopflow_inventory"
DEFAULT_PORT = 8000
_SUPPORTED_SCHEMES = ("postgresql://", "postgresql+psycopg2://")


class Settings(BaseSettings):
    """Runtime settings of the inventory service."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate.startswith(_SUPPORTED_SCHEMES):
            raise ValueError("DATABASE_URL must be a postgresql:// url")
        return candidate


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings, read once from the environment."""

    return Settings()


def load_settings() -> Settings:
    """Return the process settings (uncached variant for tests and CLI entry points)."""

    return Settings()
