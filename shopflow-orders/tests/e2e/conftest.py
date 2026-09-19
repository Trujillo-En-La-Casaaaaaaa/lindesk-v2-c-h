"""End-to-end suite: the composed ShopFlow stack through the public gateway.

The whole ecosystem must be running:

```bash
docker compose -f ../shopflow-infra/docker-compose.yml up -d --build
```

The suite only uses the public surface - the gateway (contracts.md §2) and the
provider emulator's test affordance (contracts.md §7). It never touches a database
and never calls a backend service directly, which is exactly what the black-box
acceptance criteria describe.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker  # noqa: F401 - re-exported for typing clarity

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_ROOT = REPO_ROOT.parent / "shopflow-infra"

DEFAULT_GATEWAY_BASE_URL = "http://localhost:8080"
DEFAULT_PROVIDER_BASE_URL = "http://localhost:8081"

START_COMMAND = "docker compose -f ../shopflow-infra/docker-compose.yml up -d --build"


def gateway_base_url() -> str:
    return (os.environ.get("ORDERS_E2E_GATEWAY_URL") or os.environ.get("GATEWAY_BASE_URL")
            or DEFAULT_GATEWAY_BASE_URL).rstrip("/")


def provider_base_url() -> str:
    return (
        os.environ.get("ORDERS_E2E_PROVIDER_URL")
        or os.environ.get("SHOPFLOW_PROVIDER_URL")
        or DEFAULT_PROVIDER_BASE_URL
    ).rstrip("/")


def require_reachable(url: str, *, what: str) -> None:
    try:
        response = httpx.get(f"{url}/health", timeout=5.0)
    except httpx.HTTPError as exc:
        pytest.fail(
            f"the {what} at {url} is not reachable ({type(exc).__name__}); "
            f"start the stack with\n  {START_COMMAND}"
        )
    if response.status_code != 200:
        pytest.fail(
            f"the {what} at {url} answered /health with {response.status_code}; "
            f"start the stack with\n  {START_COMMAND}"
        )


@pytest.fixture(scope="session")
def gateway() -> str:
    url = gateway_base_url()
    require_reachable(url, what="gateway")
    return url


@pytest.fixture(scope="session")
def provider() -> str:
    url = provider_base_url()
    require_reachable(url, what="provider emulator")
    return url


@pytest.fixture()
def api(gateway: str) -> Iterator[httpx.Client]:
    """A client of the public gateway, as the browser would use it."""

    with httpx.Client(base_url=gateway, timeout=30.0) as client:
        yield client


@pytest.fixture()
def emulator(provider: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=provider, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def infra_scripts() -> Path:
    scripts = INFRA_ROOT / "scripts" / "smoke_test.py"
    if not scripts.is_file():
        pytest.fail(f"the infrastructure smoke test is missing at {scripts}")
    return scripts


def run_smoke_test(
    scripts: Path, *, gateway: str, provider: str, timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    """Run shopflow-infra's black-box smoke test against the running stack."""

    environment = {
        **os.environ,
        "GATEWAY_BASE_URL": gateway,
        "SHOPFLOW_PROVIDER_URL": provider,
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and fixed script
        [sys.executable, str(scripts)],
        cwd=scripts.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


__all__ = [
    "DEFAULT_GATEWAY_BASE_URL",
    "DEFAULT_PROVIDER_BASE_URL",
    "START_COMMAND",
    "gateway_base_url",
    "provider_base_url",
    "run_smoke_test",
]
