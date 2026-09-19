"""Template tests: ``.env.example`` and the compose files must agree.

``.env.example`` is the repository's environment template (the ``infra-config``
component: "ports, credential-free local defaults, and environment templates").
A variable that is documented but never read by ``docker compose`` is a silent
no-op for the operator, and a variable that compose reads but nobody documents is
undiscoverable — both are real defects. These tests make the correspondence
bidirectional, and also check that the template covers every variable name that
``contracts.md`` §11 defines.

Static and dependency-free: no docker, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_TEMPLATE = REPO_ROOT / ".env.example"
COMPOSE_FILES = (REPO_ROOT / "docker-compose.yml", REPO_ROOT / "docker-compose.offline.yml")
README = REPO_ROOT / "README.md"

#: ``${VAR}`` or ``${VAR:-default}``, but not the ``$${VAR}`` form that is meant
#: for the container's own shell (compose does not interpolate those).
COMPOSE_REFERENCE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

#: Documented for the host-side scripts only — compose never interpolates them
#: (the compose one-shot services receive their values as literals).
HOST_ONLY_VARIABLES = frozenset({"GATEWAY_BASE_URL", "SHOPFLOW_PROVIDER_URL"})

#: Every variable name defined by contracts.md §11.
CONTRACT_VARIABLES = (
    "DATABASE_URL",
    "BROKER_URL",
    "INVENTORY_BASE_URL",
    "ORDERS_BASE_URL",
    "PROVIDER_BASE_URL",
    "REQUEST_TIMEOUT_SECONDS",
    "RELEASE_RETRY_INTERVAL_SECONDS",
)

#: Values that would suggest a real secret leaked into the template.
SECRET_MARKERS = ("-----begin", "akia", "prod-", "live-", "-production")
LONG_RANDOM = re.compile(r"[A-Za-z0-9+/]{40,}")


def compose_variables() -> set[str]:
    found: set[str] = set()
    for path in COMPOSE_FILES:
        found |= set(COMPOSE_REFERENCE.findall(path.read_text(encoding="utf-8")))
    return found


def documented_variables() -> set[str]:
    documented: set[str] = set()
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = ENV_ASSIGNMENT.match(line.strip())
        if match:
            documented.add(match.group(1))
    return documented


def documented_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = ENV_ASSIGNMENT.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def test_every_variable_compose_interpolates_is_documented() -> None:
    missing = sorted(compose_variables() - documented_variables())

    assert not missing, f"docker compose interpolates variables that .env.example does not document: {missing}"


def test_every_documented_variable_is_actually_used() -> None:
    unused = sorted(documented_variables() - compose_variables() - HOST_ONLY_VARIABLES)

    assert not unused, (
        "these variables are documented in .env.example but neither interpolated by docker compose "
        f"nor declared host-side-only: {unused}"
    )


def test_the_host_only_variables_are_genuinely_not_interpolated() -> None:
    compose = compose_variables()

    for name in sorted(HOST_ONLY_VARIABLES):
        assert name in documented_variables(), f"{name} is declared host-only but not documented"
        assert name not in compose, (
            f"{name} is described as host-side only, but docker compose interpolates it — update the comment"
        )
        # ...and the host scripts really do read them
        consumers = [path for path in (REPO_ROOT / "scripts").glob("*.py") if name in path.read_text(encoding="utf-8")]
        assert consumers, f"{name} is documented as a host-script input but no script reads it"


def test_every_contract_variable_name_is_covered() -> None:
    template = ENV_TEMPLATE.read_text(encoding="utf-8")

    for name in CONTRACT_VARIABLES:
        assert name in template, f"contracts.md §11 names {name}, which .env.example never mentions"


def test_documented_defaults_are_local_and_secret_free() -> None:
    values = documented_values()

    assert values, ".env.example assigns nothing"
    for name, value in values.items():
        assert value, f"{name} is documented with an empty value"
        lowered = value.lower()
        assert not any(marker in lowered for marker in SECRET_MARKERS), f"{name} looks like a real credential: {value!r}"
        assert not LONG_RANDOM.search(value), f"{name} looks like a generated secret: {value!r}"


def test_the_readme_documents_the_same_contract_variables() -> None:
    readme = README.read_text(encoding="utf-8")

    for name in CONTRACT_VARIABLES:
        assert name in readme, f"contracts.md §11 names {name}, which the README never mentions"


def test_the_template_is_not_git_ignored() -> None:
    """The template ships; a local ``.env`` never does."""

    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in gitignore
    assert "!.env.example" in gitignore
