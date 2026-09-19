"""Structural tests for the ``Makefile``.

``make`` is not available in every environment this repository is reviewed in (it
is absent on a stock Windows host, including this workspace), so the Makefile is
verified by parsing it instead: every documented target exists, is declared
``.PHONY``, keeps its recipes tab-indented, only references defined variables, and
expands to exactly the command the README and runbook document. Renaming a script
or editing a compose flag without updating the wrapper therefore fails the suite.

The recipes themselves are the same commands the documentation gives as the
manual equivalents, and they are exercised directly by the runbook's validation
steps.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

#: Every target the handoff's documented workflow relies on.
DOCUMENTED_TARGETS = (
    "help",
    "build",
    "up",
    "down",
    "stop",
    "reset",
    "restart",
    "migrate",
    "seed",
    "smoke",
    "smoke-in-stack",
    "health",
    "test",
    "logs",
    "ps",
)

#: What each target must run once ``$(COMPOSE)``/``$(PYTHON)`` are expanded with
#: their documented defaults.
EXPECTED_COMMANDS = {
    "build": "docker compose -f docker-compose.yml build",
    "up": "docker compose -f docker-compose.yml up -d --build",
    "down": "docker compose -f docker-compose.yml down",
    "stop": "docker compose -f docker-compose.yml stop",
    "reset": "docker compose -f docker-compose.yml down -v",
    "restart": "docker compose -f docker-compose.yml restart",
    "migrate": "python3 scripts/run_migrations.py --compose-file docker-compose.yml",
    "seed": "python3 scripts/seed.py --compose-file docker-compose.yml",
    "smoke": "python3 scripts/smoke_test.py",
    "smoke-in-stack": "docker compose -f docker-compose.yml run --rm smoke-test",
    "health": "python3 scripts/wait_for_health.py --timeout 180",
    "test": "python3 -m pytest emulator/tests scripts/tests -q",
    "logs": "docker compose -f docker-compose.yml logs -f --tail=100",
    "ps": "docker compose -f docker-compose.yml ps",
}

ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|\+=|:=|=)\s*(.*)$")
TARGET = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")
REFERENCE = re.compile(r"\$\((\w+)\)|\$\{(\w+)\}")


def parse_makefile() -> tuple[dict[str, str], dict[str, list[str]], list[str], dict[str, str]]:
    """Return ``(variables, recipes_by_target, target_order, inline_prerequisites)``."""

    variables: dict[str, str] = {}
    recipes: dict[str, list[str]] = {}
    order: list[str] = []
    prerequisites: dict[str, str] = {}
    current: str | None = None

    for number, line in enumerate(MAKEFILE.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("\t"):
            assert current is not None, f"Makefile:{number}: recipe line outside any target: {line!r}"
            recipes[current].append(line[1:])
            continue

        if line.startswith(" ") and line.strip():
            pytest.fail(f"Makefile:{number}: recipes must be tab-indented, found {line!r}")

        if not line.strip() or line.lstrip().startswith("#"):
            current = None
            continue

        assignment = ASSIGNMENT.match(line)
        if assignment:
            variables[assignment.group(1)] = assignment.group(3).strip()
            current = None
            continue

        target = TARGET.match(line)
        if target:
            current = target.group(1)
            recipes.setdefault(current, [])
            prerequisites[current] = target.group(2).strip()
            if current != ".PHONY":
                order.append(current)
            continue

        current = None

    return variables, recipes, order, prerequisites


def expand(line: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        assert name in variables, f"undefined make variable $({name}) in {line!r}"
        return variables[name]

    return REFERENCE.sub(replace, line)


@pytest.fixture(scope="module")
def makefile() -> tuple[dict[str, str], dict[str, list[str]], list[str], dict[str, str]]:
    return parse_makefile()


def test_every_documented_target_exists(makefile) -> None:
    _, recipes, order, _ = makefile

    assert set(order) == set(DOCUMENTED_TARGETS), "the target set changed; update the docs too"
    for target in DOCUMENTED_TARGETS:
        assert recipes[target], f"target '{target}' has no recipe"


def test_targets_are_phony_so_no_file_can_shadow_them(makefile) -> None:
    _, _, _, prerequisites = makefile

    phony = prerequisites[".PHONY"].split()

    missing = [target for target in DOCUMENTED_TARGETS if target not in phony]
    assert not missing, f"targets missing from .PHONY: {missing}"


def test_every_variable_used_is_defined(makefile) -> None:
    variables, recipes, _, _ = makefile

    for name in ("COMPOSE", "PYTHON", "COMPOSE_FILE"):
        assert name in variables, f"{name} must have a documented default"

    for target, lines in recipes.items():
        for line in lines:
            expand(line, variables)  # asserts on any undefined reference


@pytest.mark.parametrize(("target", "expected"), sorted(EXPECTED_COMMANDS.items()))
def test_target_expands_to_the_documented_command(makefile, target: str, expected: str) -> None:
    variables, recipes, _, _ = makefile

    expanded = [expand(line, variables) for line in recipes[target] if not line.startswith("@")]

    assert expanded == [expected]


def test_help_lists_every_target(makefile) -> None:
    variables, recipes, _, _ = makefile

    help_text = " ".join(expand(line, variables) for line in recipes["help"])

    for target in DOCUMENTED_TARGETS:
        assert target in help_text, f"'{target}' is not mentioned by `make help`"


def test_the_files_the_targets_reference_exist(makefile) -> None:
    variables, recipes, _, _ = makefile

    referenced = {
        "docker-compose.yml",
        "scripts/run_migrations.py",
        "scripts/seed.py",
        "scripts/smoke_test.py",
        "scripts/wait_for_health.py",
        "emulator/tests",
        "scripts/tests",
    }
    assert variables["COMPOSE_FILE"] == "docker-compose.yml"

    for path in sorted(referenced):
        assert (REPO_ROOT / path).exists(), f"{path} is referenced by the Makefile but missing"

    # every expanded recipe only mentions paths that exist
    for target in ("migrate", "seed", "smoke", "health", "test"):
        line = " ".join(expand(entry, variables) for entry in recipes[target])
        for token in line.split():
            if "/" in token and not token.startswith("-") and not token.startswith("compose-file"):
                assert (REPO_ROOT / token).exists(), f"{target}: {token} does not exist"
