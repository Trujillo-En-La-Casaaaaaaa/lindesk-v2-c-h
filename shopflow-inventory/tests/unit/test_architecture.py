"""Architecture tests: the hexagonal boundary is real (handoff acceptance 8).

``app/domain`` and ``app/usecases`` must not import ``fastapi`` or ``sqlalchemy``.
The test proves it twice: statically (import statements parsed from the sources)
and dynamically (the modules are importable in a fresh interpreter where the
frameworks are made unimportable).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGES = ("app/domain", "app/usecases", "app/ports")
#: Frameworks and drivers that must never be reachable from the core.
FORBIDDEN_TOP_LEVEL = ("fastapi", "starlette", "sqlalchemy", "alembic", "uvicorn", "psycopg2", "psycopg", "pydantic")
#: The only non-standard-library packages the core may import.
ALLOWED_CORE_PREFIXES = ("app.domain", "app.ports", "app.usecases")


def core_modules() -> list[Path]:
    modules: list[Path] = []
    for package in CORE_PACKAGES:
        modules.extend(sorted((REPO_ROOT / package).glob("*.py")))
    return modules


def imported_modules(path: Path) -> set[str]:
    """Return the imported module paths (dotted, as written) of one source file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    return imported


def relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_core_modules_exist() -> None:
    assert {relative(path) for path in core_modules()} >= {
        "app/domain/errors.py",
        "app/domain/product.py",
        "app/domain/stock.py",
        "app/ports/repositories.py",
        "app/usecases/availability.py",
        "app/usecases/catalog.py",
        "app/usecases/release.py",
        "app/usecases/reserve.py",
    }


@pytest.mark.parametrize("path", core_modules(), ids=relative)
def test_core_sources_do_not_import_frameworks(path: Path) -> None:
    forbidden = sorted(
        module for module in imported_modules(path) if module.split(".")[0] in FORBIDDEN_TOP_LEVEL
    )

    assert not forbidden, f"{relative(path)} must stay framework-free, but imports {forbidden}"


@pytest.mark.parametrize("path", core_modules(), ids=relative)
def test_core_sources_import_only_the_core_and_the_standard_library(path: Path) -> None:
    outer = sorted(
        module
        for module in imported_modules(path)
        if module.split(".")[0] not in sys.stdlib_module_names
        and not module.startswith(ALLOWED_CORE_PREFIXES)
    )

    assert not outer, f"{relative(path)} must not depend on outer layers, but imports {outer}"


def test_core_imports_in_an_interpreter_without_frameworks() -> None:
    """Importing the core must not need FastAPI, SQLAlchemy or pydantic at all."""

    script = """
import importlib
import sys

BLOCKED = {"fastapi", "starlette", "sqlalchemy", "pydantic", "pydantic_settings", "alembic", "uvicorn"}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"blocked import of {name}")
        return None


sys.meta_path.insert(0, Blocker())
for module in (
    "app.domain.errors",
    "app.domain.product",
    "app.domain.stock",
    "app.ports.repositories",
    "app.usecases.availability",
    "app.usecases.catalog",
    "app.usecases.release",
    "app.usecases.reserve",
):
    importlib.import_module(module)
print("core-import-ok")
"""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, trusted inline script
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "core-import-ok" in completed.stdout
