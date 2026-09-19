"""Run this repository's own Alembic tooling against a store (test helper)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``alembic`` with ``DATABASE_URL`` pointing at the store under test."""

    return subprocess.run(  # noqa: S603 - fixed interpreter and fixed alembic arguments
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )


__all__ = ["REPO_ROOT", "run_alembic"]
