"""Acceptance criterion 12: the core must not import a framework.

``app/domain`` and ``app/usecases`` may only depend on the Python standard library
and on the ports. The check parses every module with :mod:`ast` (so it looks at the
real imports, not at a text match) and asserts that no web framework, ORM, database
driver or broker client appears. The same rule is applied to ``app/ports`` and
``app/events``; the adapters, the API layer, the workers and the composition root are
where those libraries are allowed - and the last test proves the scan actually sees
them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "app"

PURE_PACKAGES = ("domain", "usecases", "ports", "events")

#: Modules the core must never import (HANDOFF #8 and acceptance criterion 12).
FORBIDDEN_ROOTS = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "pika",
    "httpx",
    "alembic",
    "psycopg2",
    "uvicorn",
    "pydantic",
    "pydantic_settings",
}

ALLOWED_APP_PREFIXES = ("app.domain", "app.ports", "app.events", "app.usecases")
#: The core must not reach into the infrastructure or the composition root.
FORBIDDEN_APP_PREFIXES = ("app.adapters", "app.api", "app.db", "app.main", "app.workers")


def module_files(package: str) -> list[Path]:
    return sorted(path for path in (APP / package).rglob("*.py") if path.name != "__init__.py")


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("package", PURE_PACKAGES)
def test_core_packages_do_not_import_infrastructure(package: str) -> None:
    files = module_files(package)
    assert files, f"no module found in app/{package}"

    offenders: dict[str, set[str]] = {}
    for path in files:
        bad = {root for root in imported_roots(path) if root in FORBIDDEN_ROOTS}
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert offenders == {}, f"app/{package} must stay framework free, found {offenders}"


def test_domain_and_usecases_only_depend_on_the_domain_and_the_ports() -> None:
    offenders: dict[str, set[str]] = {}
    for package in ("domain", "usecases"):
        for path in module_files(package):
            external = {
                module
                for module in imported_modules(path)
                if module.startswith("app.")
                and not module.startswith(ALLOWED_APP_PREFIXES)
            }
            if external:
                offenders[str(path.relative_to(REPO_ROOT))] = external
            layers = {module for module in imported_modules(path) if module.startswith(FORBIDDEN_APP_PREFIXES)}
            assert layers == set(), f"{path} must not import {sorted(layers)}"
    assert offenders == {}, (
        "the core may only import app.domain/app.ports/app.events/app.usecases, "
        f"found {offenders}"
    )


def test_ports_and_events_are_pure_python() -> None:
    for package in ("ports", "events"):
        for path in module_files(package):
            roots = imported_roots(path)
            assert not (roots & FORBIDDEN_ROOTS), f"{path} imports {sorted(roots & FORBIDDEN_ROOTS)}"


def test_the_scan_would_notice_a_violation() -> None:
    """The rule above is only meaningful if the scan can see a real import."""

    adapter = APP / "adapters" / "inventory_http_client.py"
    assert "httpx" in imported_roots(adapter)
    composition_root = APP / "main.py"
    assert "fastapi" in imported_roots(composition_root)
    assert {"app.db.session", "app.adapters.postgres_order_repository"} <= imported_modules(composition_root)
    assert "sqlalchemy" in imported_roots(APP / "adapters" / "postgres_order_repository.py")
    ports = APP / "ports" / "outbox.py"
    assert imported_roots(ports) <= {"__future__", "uuid", "collections", "dataclasses", "datetime", "typing", "app"}
