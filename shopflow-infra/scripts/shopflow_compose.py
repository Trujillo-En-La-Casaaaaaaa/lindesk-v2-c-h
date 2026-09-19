"""Thin ``docker compose`` wrapper shared by the infra job scripts.

``shopflow-infra`` never runs a service's migration or seed logic itself: it asks
docker compose to run the owning service's own one-shot job container, so the
business code always executes inside the repository that owns it (HANDOFF #5/#6).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


class ComposeError(RuntimeError):
    """The compose CLI or a compose job could not be run."""


def docker_executable() -> str:
    """Return the docker CLI to use (``SHOPFLOW_DOCKER`` overrides discovery)."""

    configured = os.environ.get("SHOPFLOW_DOCKER", "").strip()
    if configured:
        return configured
    found = shutil.which("docker")
    if not found:
        raise ComposeError("the 'docker' CLI was not found on PATH; install Docker or set SHOPFLOW_DOCKER")
    return found


def compose_file_from(path: str | None) -> Path:
    """Resolve the compose file, defaulting to this repository's ``docker-compose.yml``."""

    candidate = Path(path).expanduser() if path else DEFAULT_COMPOSE_FILE
    resolved = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    resolved = resolved.resolve()
    if not resolved.is_file():
        raise ComposeError(f"compose file not found: {resolved}")
    return resolved


def base_command(compose_file: Path) -> list[str]:
    """The ``docker compose -f <file>`` prefix."""

    return [docker_executable(), "compose", "-f", str(compose_file)]


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run_job(
    service: str,
    *,
    compose_file: Path,
    run_flags: Iterable[str] = (),
    dry_run: bool = False,
) -> int:
    """Run one compose one-shot job service and return its exit code.

    The job service carries its own entrypoint/command and environment (see
    ``docker-compose.yml``), so nothing about the job is re-implemented here.
    """

    command = base_command(compose_file) + ["run", "--rm", "-T", *run_flags, service]
    print(f"+ {format_command(command)}", flush=True)
    if dry_run:
        print("  (dry run: not executed)", flush=True)
        return 0

    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:  # docker vanished mid-run
        raise ComposeError(f"could not run {format_command(command)}: {exc}") from exc
    return int(completed.returncode)


def describe_effective_command(env_var: str, default: str) -> str:
    """Return the command the job container will run (for operator output)."""

    value = os.environ.get(env_var, "").strip()
    return value or default


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


__all__ = [
    "ComposeError",
    "DEFAULT_COMPOSE_FILE",
    "REPO_ROOT",
    "base_command",
    "compose_file_from",
    "describe_effective_command",
    "docker_executable",
    "fail",
    "format_command",
    "run_job",
]
