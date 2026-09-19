#!/usr/bin/env python
"""Run every ShopFlow service's own migrations in dependency order (HANDOFF #5).

Order: ``inventory``, ``orders``, ``notifications``.

Each step runs the owning service's own migration command inside its own image
(``alembic upgrade head`` by default, overridable per service with the
environment variables below), so no schema is ever created by infrastructure
code. Every step is idempotent, and the script stops at the first failure so a
half-migrated stack is never reported as ready.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shopflow_compose as compose  # noqa: E402  (path bootstrap above)


@dataclass(frozen=True)
class MigrationJob:
    """One service's migration step."""

    name: str
    service: str
    env_var: str
    default_command: str


#: Dependency order from the handoff: inventory, orders, notifications.
JOBS: tuple[MigrationJob, ...] = (
    MigrationJob("inventory", "migrate-inventory", "INVENTORY_MIGRATION_COMMAND", "alembic upgrade head"),
    MigrationJob("orders", "migrate-orders", "ORDERS_MIGRATION_COMMAND", "alembic upgrade head"),
    MigrationJob("notifications", "migrate-notifications", "NOTIFICATIONS_MIGRATION_COMMAND", "alembic upgrade head"),
)


def selected_jobs(only: str | None) -> list[MigrationJob]:
    if not only:
        return list(JOBS)
    requested = [item.strip() for item in only.split(",") if item.strip()]
    by_name = {job.name: job for job in JOBS}
    unknown = [name for name in requested if name not in by_name]
    if unknown:
        raise ValueError(f"unknown service(s) {unknown}; expected one of {sorted(by_name)}")
    return [by_name[name] for name in requested]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply each ShopFlow service's migrations through its own migration tool.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--compose-file", default=None, help="path to docker-compose.yml")
    parser.add_argument("--only", default=None, help="comma-separated subset, e.g. 'inventory,orders'")
    parser.add_argument("--build", action="store_true", help="rebuild the job images before running them")
    parser.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        compose_file = compose.compose_file_from(args.compose_file)
        jobs = selected_jobs(args.only)
    except (compose.ComposeError, ValueError) as exc:
        return compose.fail(str(exc))

    print(f"migrations: {len(jobs)} step(s) via {compose_file}")
    run_flags = ["--build"] if args.build else []

    for index, job in enumerate(jobs, start=1):
        effective = compose.describe_effective_command(job.env_var, job.default_command)
        print(f"\n[{index}/{len(jobs)}] {job.name}: {effective}", flush=True)
        try:
            exit_code = compose.run_job(job.service, compose_file=compose_file, run_flags=run_flags, dry_run=args.dry_run)
        except compose.ComposeError as exc:
            return compose.fail(str(exc))
        if exit_code != 0:
            print(f"migrations: FAILED at '{job.name}' (exit code {exit_code})", file=sys.stderr)
            return exit_code or 1

    applied = ", ".join(job.name for job in jobs)
    print(f"\nmigrations: OK - {applied} up to head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
