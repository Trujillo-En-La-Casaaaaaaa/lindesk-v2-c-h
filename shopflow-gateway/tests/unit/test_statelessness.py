"""The gateway owns no durable data and depends on no database client (AC 9)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Packages that would indicate a durable store in the gateway image.
DATABASE_CLIENTS = (
    "psycopg",
    "psycopg2",
    "asyncpg",
    "aiopg",
    "sqlalchemy",
    "alembic",
    "sqlmodel",
    "redis",
    "pymongo",
    "motor",
    "mysql",
    "aiomysql",
    "pymysql",
    "aiosqlite",
    "cassandra",
    "elasticsearch",
    "boto3",
    "minio",
)

REQUIREMENT_LINE = re.compile(r"^[A-Za-z0-9._-]+==[^\s;]+")


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-dev.txt"])
def test_lockfiles_are_fully_pinned(filename: str) -> None:
    lines = _requirement_lines(REPO_ROOT / filename)

    assert lines, f"{filename} must not be empty"
    for line in lines:
        if filename == "requirements-dev.txt" and line.startswith("-r "):
            continue
        assert REQUIREMENT_LINE.match(line), f"{filename} line is not pinned: {line}"


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-dev.txt"])
def test_lockfiles_contain_no_database_client(filename: str) -> None:
    text = (REPO_ROOT / filename).read_text(encoding="utf-8").lower()

    found = [package for package in DATABASE_CLIENTS if package in text]
    assert found == [], f"{filename} declares database clients: {found}"


def test_pyproject_declares_no_database_client() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    found = [package for package in DATABASE_CLIENTS if package in text]
    assert found == [], f"pyproject.toml declares database clients: {found}"


def test_gateway_source_imports_no_database_client() -> None:
    offenders: list[str] = []
    for module in (REPO_ROOT / "app").rglob("*.py"):
        text = module.read_text(encoding="utf-8").lower()
        for package in DATABASE_CLIENTS:
            if re.search(rf"^\s*(import|from)\s+{re.escape(package)}\b", text, flags=re.MULTILINE):
                offenders.append(f"{module.relative_to(REPO_ROOT)} imports {package}")
    assert offenders == []


def test_settings_expose_no_store_configuration() -> None:
    keys = Settings().model_dump()

    assert not [key for key in keys if "database" in key.lower() or "broker" in key.lower()]
    assert "DATABASE_URL" not in {key.upper() for key in keys}


def test_application_state_holds_no_store_handle() -> None:
    app = create_app(settings=Settings())
    state = dict(app.state._state)  # noqa: SLF001 - inspect the app state container

    assert set(state) == {"settings", "route_registry"}
    assert not any(key == "db" or "database" in key or "engine" in key or "session" in key for key in state)


def test_importing_the_gateway_does_not_import_a_database_driver() -> None:
    probe = (
        "import sys; import app.main; "
        f"names = {list(DATABASE_CLIENTS)!r}; "
        "print([name for name in names if name in sys.modules])"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]", completed.stdout
