"""Dependency tests: the infra scripts stay on the Python standard library.

The documented validation commands run the scripts as plain ``python scripts/x.py``
with no installation step, so a third-party import in ``scripts/*.py`` would break
them. ``httpx`` is the single optional exception (pinned in
``scripts/requirements.txt``, picked up automatically when present).

The scripts also drive the ecosystem by shelling out to the ``docker`` CLI rather
than through a Docker SDK, which is part of how they stay free of business logic:
they ask compose to run each service's own job container.

Static: parses the files, imports nothing from them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((REPO_ROOT / "scripts").glob("*.py"))

STDLIB_MODULES = set(sys.stdlib_module_names)
#: Optional transport, pinned in scripts/requirements.txt and imported defensively.
OPTIONAL_THIRD_PARTY = frozenset({"httpx"})
#: Docker SDKs the scripts must never use (they shell out to the compose CLI).
FORBIDDEN_THIRD_PARTY = frozenset({"docker", "dockerpty", "kubernetes", "podman"})


def imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by ``path``."""

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def sibling_modules(path: Path) -> set[str]:
    """Helper modules that live next to the script (not third-party)."""

    return {sibling.stem for sibling in path.parent.glob("*.py")}


def test_the_scripts_exist_to_check() -> None:
    assert {path.name for path in SCRIPTS} >= {
        "wait_for_health.py",
        "run_migrations.py",
        "seed.py",
        "smoke_test.py",
    }


def test_scripts_only_import_the_standard_library() -> None:
    offenders: dict[str, list[str]] = {}

    for path in SCRIPTS:
        local = sibling_modules(path)
        external = {
            name
            for name in imported_modules(path)
            if name not in STDLIB_MODULES and name not in OPTIONAL_THIRD_PARTY and name not in local
        }
        if external:
            offenders[path.name] = sorted(external)

    assert not offenders, f"these scripts need packages that are not installed by default: {offenders}"


def test_the_optional_dependency_is_imported_defensively() -> None:
    helper = (REPO_ROOT / "scripts" / "shopflow_http.py").read_text(encoding="utf-8")

    assert "import httpx" in helper
    assert "except Exception" in helper, "httpx must be optional, so the import has to be guarded"
    assert "urllib.request" in helper, "the standard library transport must remain the fallback"


def test_scripts_never_use_a_container_sdk() -> None:
    for path in SCRIPTS:
        used = imported_modules(path) & FORBIDDEN_THIRD_PARTY

        assert not used, f"{path.name} uses {sorted(used)}; infra must drive the compose CLI instead"


def test_the_cli_runner_wraps_docker_compose() -> None:
    runner = (REPO_ROOT / "scripts" / "shopflow_compose.py").read_text(encoding="utf-8")

    assert '"compose"' in runner and '"run"' in runner
    assert "subprocess" in runner


def test_the_requirements_lockfile_is_pinned_and_documents_the_fallback() -> None:
    lockfile = (REPO_ROOT / "scripts" / "requirements.txt").read_text(encoding="utf-8")

    pinned = [line.strip() for line in lockfile.splitlines() if line.strip() and not line.startswith("#")]
    assert pinned, "the scripts lockfile pins nothing"
    for line in pinned:
        assert "==" in line, f"every entry must be pinned exactly: {line!r}"
    assert "httpx==" in lockfile
    assert "standard library" in lockfile, "the lockfile should explain that the scripts need no third-party code"
