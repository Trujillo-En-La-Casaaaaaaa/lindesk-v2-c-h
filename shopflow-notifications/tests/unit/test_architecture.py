"""Acceptance criterion 8: the architecture rules, checked statically and at runtime.

* ``app/domain`` and ``app/handlers`` must not import FastAPI, SQLAlchemy, Alembic,
  psycopg2, the broker client (pika), httpx or uvicorn - neither directly nor
  transitively (the transitive half is proven in a fresh interpreter).
* ``app/handlers`` may only depend on the domain and the ports: never on an
  adapter, the database layer, the consumer, the API layer or the composition root.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "app"

#: Frameworks, drivers and clients that must stay out of the hexagon's core.
INFRASTRUCTURE_ROOTS = frozenset(
    {
        "fastapi",
        "starlette",
        "sqlalchemy",
        "alembic",
        "pika",
        "psycopg2",
        "httpx",
        "uvicorn",
        "pydantic",
        "pydantic_settings",
    }
)

DOMAIN_ALLOWED_APP_PREFIXES = ("app.domain",)
HANDLERS_ALLOWED_APP_PREFIXES = ("app.domain", "app.ports", "app.handlers")

RUNTIME_PROBE = (
    "import json, sys;"
    "import app.domain.notification, app.domain.templates, app.domain.retry,"
    " app.handlers.orchestrator, app.handlers.confirmation, app.handlers.cancellation;"
    "forbidden = {'fastapi','starlette','sqlalchemy','alembic','pika','psycopg2','httpx','uvicorn',"
    " 'pydantic','pydantic_settings'};"
    "print(json.dumps(sorted({name.split('.')[0] for name in sys.modules} & forbidden)))"
)


def python_files(package: str) -> list[Path]:
    return sorted(path for path in (APP / package).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Return every absolute module named by an import statement of ``path``."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            modules.add(node.module)
    return modules


def app_imports(modules: set[str]) -> set[str]:
    return {module for module in modules if module == "app" or module.startswith("app.")}


DIGEST_FILES = python_files("domain") + python_files("handlers")


@pytest.mark.parametrize("path", DIGEST_FILES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_domain_and_handlers_do_not_import_infrastructure(path: Path) -> None:
    """Neither ``domain/`` nor ``handlers/`` may name a framework or a client."""

    forbidden = {
        module
        for module in imported_modules(path)
        if module.split(".")[0] in INFRASTRUCTURE_ROOTS
    }
    assert not forbidden, f"{path.relative_to(REPO_ROOT)} imports infrastructure: {sorted(forbidden)}"


@pytest.mark.parametrize("path", python_files("domain"), ids=lambda path: path.name)
def test_domain_depends_on_the_domain_only(path: Path) -> None:
    """The domain core has no outbound dependency inside this repository."""

    used = app_imports(imported_modules(path))
    unexpected = {
        module
        for module in used
        if not any(module == prefix or module.startswith(f"{prefix}.") for prefix in DOMAIN_ALLOWED_APP_PREFIXES)
    }
    assert not unexpected, f"{path.name} reaches outside the domain: {sorted(unexpected)}"


@pytest.mark.parametrize("path", python_files("handlers"), ids=lambda path: path.name)
def test_handlers_depend_on_ports_not_adapters(path: Path) -> None:
    """Handlers depend on the ports; only the composition root knows the adapters."""

    used = app_imports(imported_modules(path))
    unexpected = {
        module
        for module in used
        if not any(module == prefix or module.startswith(f"{prefix}.") for prefix in HANDLERS_ALLOWED_APP_PREFIXES)
    }
    assert not unexpected, f"{path.name} reaches outside domain/ports: {sorted(unexpected)}"


def test_handlers_actually_use_both_ports() -> None:
    """The orchestration is expressed in the port vocabulary, not in adapters."""

    used = imported_modules(APP / "handlers" / "orchestrator.py")
    assert "app.ports.repository" in used
    assert "app.ports.provider" in used


def test_no_infrastructure_module_is_loaded_at_runtime() -> None:
    """A fresh interpreter importing the core must not pull in any framework."""

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed script
        [sys.executable, "-c", RUNTIME_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout.strip().splitlines()[-1])
    assert loaded == [], f"importing the core loads infrastructure modules: {loaded}"
