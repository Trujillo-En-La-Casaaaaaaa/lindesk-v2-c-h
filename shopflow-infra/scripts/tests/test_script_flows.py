"""Flow tests for the infra scripts' ``main()`` entry points.

These cover the orchestration the scripts exist for, not just their pure helpers:

* ``run_migrations.py`` applies **inventory → orders → notifications** and stops at
  the first failing step;
* ``seed.py`` invokes the inventory-owned seed command twice and only reports
  success when both snapshots are identical (acceptance criterion 3), and never
  writes data itself;
* ``wait_for_health.py`` resolves its endpoint list (arguments *and*
  ``SHOPFLOW_HEALTH_URLS``) and maps outcomes to the documented exit codes;
* ``shopflow_compose.run_job`` shells out to ``docker compose run --rm -T <job>``,
  which is how infrastructure re-uses each service's own migration/seed command
  instead of re-implementing it.

No docker daemon and no service is touched: the compose runner and the HTTP
transport are replaced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import run_migrations
import seed
import shopflow_compose as compose
import shopflow_http as http
import wait_for_health


def catalogue(*, stock: dict[str, int] | None = None) -> dict[str, dict[str, Any]]:
    """The documented five products (§10), optional stock overrides."""

    overrides = stock or {}
    return {
        product.product_id: {
            "productId": product.product_id,
            "sku": product.sku,
            "name": product.name,
            "price": float(product.price),
            "availableQuantity": overrides.get(product.product_id, product.seeded_stock),
        }
        for product in seed.SEEDED_PRODUCTS
    }


class InventoryStub:
    """Serves ``GET /products`` from a list of snapshots, one per call."""

    def __init__(self, *snapshots: dict[str, dict[str, Any]], fail: bool = False) -> None:
        self.snapshots = list(snapshots) or [catalogue()]
        self.fail = fail
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> http.Response:
        self.calls += 1
        if self.fail:
            raise http.TransportError(f"GET {url} failed: connection refused (test)")
        snapshot = self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]
        payload = json.dumps({"items": list(snapshot.values()), "total": len(snapshot)})
        return http.Response(200, payload, {"content-type": "application/json"}, url)


class ComposeRunner:
    """Stands in for ``shopflow_compose.run_job`` and records every invocation."""

    def __init__(self, results: list[int] | None = None) -> None:
        self.calls: list[tuple[str, tuple[str, ...], bool]] = []
        self.results = list(results or [])

    def __call__(
        self,
        service: str,
        *,
        compose_file: Any = None,
        run_flags: Any = (),
        dry_run: bool = False,
    ) -> int:
        self.calls.append((service, tuple(run_flags), dry_run))
        index = len(self.calls) - 1
        return self.results[index] if index < len(self.results) else 0


@pytest.fixture()
def runner(monkeypatch: pytest.MonkeyPatch):
    def install(results: list[int] | None = None) -> ComposeRunner:
        stub = ComposeRunner(results)
        monkeypatch.setattr(compose, "run_job", stub)
        return stub

    return install


@pytest.fixture()
def inventory(monkeypatch: pytest.MonkeyPatch):
    def install(*snapshots: dict[str, dict[str, Any]], fail: bool = False) -> InventoryStub:
        stub = InventoryStub(*snapshots, fail=fail)
        monkeypatch.setattr(http, "request", stub.request)
        return stub

    return install


# --------------------------------------------------------------------------- #
# run_migrations.py (HANDOFF #5)
# --------------------------------------------------------------------------- #
def test_migrations_apply_every_service_in_dependency_order(runner) -> None:
    runs = runner()

    assert run_migrations.main([]) == 0

    assert [call[0] for call in runs.calls] == ["migrate-inventory", "migrate-orders", "migrate-notifications"]


def test_migrations_stop_at_the_first_failure(runner) -> None:
    runs = runner([0, 3])

    assert run_migrations.main([]) == 3

    assert [call[0] for call in runs.calls] == ["migrate-inventory", "migrate-orders"]


def test_migrations_subset_build_and_dry_run_flags_are_forwarded(runner) -> None:
    runs = runner([0])

    assert run_migrations.main(["--only", "orders", "--build", "--dry-run"]) == 0

    assert runs.calls == [("migrate-orders", ("--build",), True)]


def test_migrations_reject_an_unknown_service(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_migrations.main(["--only", "payments"]) == 1
    assert "payments" in capsys.readouterr().err


def test_migrations_fail_cleanly_when_docker_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SHOPFLOW_DOCKER", raising=False)
    monkeypatch.setattr(compose.shutil, "which", lambda _name: None)

    assert run_migrations.main([]) == 1
    assert "docker" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# seed.py (HANDOFF #6, acceptance criterion 3)
# --------------------------------------------------------------------------- #
def test_seed_runs_the_owned_command_twice_and_proves_idempotency(runner, inventory, capsys) -> None:
    runs = runner([0, 0])
    inventory(catalogue(), catalogue())

    assert seed.main(["--inventory-url", "http://inventory.test"]) == 0

    out = capsys.readouterr().out
    assert [call[0] for call in runs.calls] == ["seed-inventory", "seed-inventory"]
    assert "idempotent" in out
    for product in seed.SEEDED_PRODUCTS:
        assert product.product_id in out


def test_seed_fails_when_the_second_run_changes_state(runner, inventory, capsys) -> None:
    runner([0, 0])
    inventory(catalogue(), catalogue(stock={"p-1001": 49}))

    assert seed.main(["--inventory-url", "http://inventory.test"]) == 1

    err = capsys.readouterr().err
    assert "FAILED" in err and "p-1001" in err


def test_seed_fails_when_the_owned_command_fails(runner, inventory) -> None:
    runs = runner([2])
    inventory()

    assert seed.main(["--inventory-url", "http://inventory.test"]) == 2

    assert len(runs.calls) == 1, "a failed seed must not be followed by a second run"


def test_seed_rejects_a_catalogue_that_does_not_match_the_contract(runner, inventory, capsys) -> None:
    runner([0])
    incomplete = catalogue()
    del incomplete["p-1005"]
    inventory(incomplete)

    assert seed.main(["--inventory-url", "http://inventory.test"]) == 1
    assert "p-1005" in capsys.readouterr().err


def test_seed_warns_when_verification_is_impossible_and_strict_turns_it_into_a_failure(
    runner, inventory, capsys
) -> None:
    runner([0, 0, 0, 0])
    inventory(fail=True)

    assert seed.main(["--inventory-url", "http://inventory.test"]) == 0
    assert "WARNING" in capsys.readouterr().out

    assert seed.main(["--inventory-url", "http://inventory.test", "--strict"]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_seed_no_verify_runs_the_owned_command_once(runner, inventory) -> None:
    runs = runner([0])
    inventory()

    assert seed.main(["--no-verify"]) == 0

    assert len(runs.calls) == 1


def test_seed_dry_run_touches_nothing(runner, capsys) -> None:
    runs = runner([0])

    assert seed.main(["--dry-run"]) == 0

    assert "dry run" in capsys.readouterr().out
    assert runs.calls == [("seed-inventory", (), True)]


# --------------------------------------------------------------------------- #
# wait_for_health.py (HANDOFF #7)
# --------------------------------------------------------------------------- #
def test_health_gate_reads_the_endpoint_list_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setenv(wait_for_health.URLS_ENV_VAR, "emulator=http://provider.test/health")
    monkeypatch.setattr(
        wait_for_health,
        "probe",
        lambda target, request_timeout: (seen.append(target.url) or True, "HTTP 200"),
    )

    assert wait_for_health.main(["--timeout", "1", "--quiet"]) == 0
    assert seen == ["http://provider.test/health"]


def test_health_gate_reports_the_unhealthy_endpoints(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(wait_for_health, "probe", lambda target, request_timeout: (False, "unreachable"))

    assert wait_for_health.main(["--urls", "gateway=http://a/health", "--timeout", "1", "--interval", "1", "--quiet"]) == 1

    err = capsys.readouterr().err
    assert "NOT READY" in err
    assert "FAIL gateway" in err and "unreachable" in err


def test_health_gate_rejects_a_malformed_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert wait_for_health.main(["--urls", "not-a-url"]) == 2
    assert "invalid endpoint" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# shopflow_compose.run_job — infrastructure never re-implements a service job
# --------------------------------------------------------------------------- #
class Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_run_job_shells_out_to_docker_compose_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, check=False):  # noqa: ANN001, ANN202 - mirrors subprocess.run
        captured["argv"] = list(argv)
        return Completed(0)

    monkeypatch.setattr(compose, "docker_executable", lambda: "docker")
    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    assert compose.run_job("migrate-inventory", compose_file=compose.DEFAULT_COMPOSE_FILE) == 0

    assert captured["argv"] == [
        "docker",
        "compose",
        "-f",
        str(compose.DEFAULT_COMPOSE_FILE),
        "run",
        "--rm",
        "-T",
        "migrate-inventory",
    ]


def test_run_job_forwards_extra_flags_and_propagates_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, check=False):  # noqa: ANN001, ANN202
        captured["argv"] = list(argv)
        return Completed(7)

    monkeypatch.setattr(compose, "docker_executable", lambda: "docker")
    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    assert compose.run_job(
        "migrate-orders",
        compose_file=compose.DEFAULT_COMPOSE_FILE,
        run_flags=["--build"],
    ) == 7
    assert captured["argv"][-2:] == ["--build", "migrate-orders"]


def test_run_job_dry_run_executes_nothing(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("dry run must not execute anything")

    monkeypatch.setattr(compose, "docker_executable", lambda: "docker")
    monkeypatch.setattr(compose.subprocess, "run", explode)

    assert compose.run_job("seed-inventory", compose_file=compose.DEFAULT_COMPOSE_FILE, dry_run=True) == 0
    assert "dry run" in capsys.readouterr().out


def test_docker_executable_honours_the_override_and_reports_a_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOPFLOW_DOCKER", "/custom/docker")
    assert compose.docker_executable() == "/custom/docker"

    monkeypatch.delenv("SHOPFLOW_DOCKER", raising=False)
    monkeypatch.setattr(compose.shutil, "which", lambda _name: None)
    with pytest.raises(compose.ComposeError):
        compose.docker_executable()
