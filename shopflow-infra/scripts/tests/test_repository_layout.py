"""Layout tests: the handoff's required files exist, and the README says so truthfully.

The handoff prescribes a required repository layout (README, compose file,
``.env.example``, ``.gitignore``, a Makefile *or* the wrapper scripts, the
emulator service with its tests, the four infra scripts and the runbook). This
suite turns that requirement into an assertion, and additionally checks that the
"Repository layout" section of the README lists only paths that really exist —
so documentation drift fails the build instead of misleading a reader.

Static and dependency-free: no docker, no network, no service.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

#: Paths the handoff requires to exist (any of ``*_alternatives`` satisfies it).
REQUIRED_FILES = (
    "README.md",
    "docker-compose.yml",
    ".env.example",
    ".gitignore",
    "emulator/Dockerfile",
    "emulator/requirements.txt",
    "emulator/app/main.py",
    "emulator/app/store.py",
    "emulator/tests/test_emulator.py",
    "scripts/wait_for_health.py",
    "scripts/run_migrations.py",
    "scripts/seed.py",
    "scripts/smoke_test.py",
    "docs/runbook.md",
)

REQUIRED_ALTERNATIVE_GROUPS = (("Makefile", "up.sh", "up.ps1"),)


def readme_layout_block() -> list[str]:
    """Return the lines of the README's fenced repository-layout block."""

    text = README.read_text(encoding="utf-8")
    marker = text.index("## 11. Repository layout")
    opening = text.index("```", marker)
    closing = text.index("```", opening + 3)
    return text[opening + 3 : closing].splitlines()


def layout_paths() -> list[str]:
    """Expand the layout tree into concrete repository-relative paths."""

    paths: list[str] = []
    prefix = ""
    for line in readme_layout_block():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        if indent == 0:
            continue  # the tree root line (e.g. "shopflow-infra/")
        if indent <= 2:
            prefix = ""
        if body.endswith("/"):
            prefix = body
            paths.append(body.rstrip("/"))
            continue
        for token in re.split(r"[,\s]+| / ", body):
            token = token.strip().strip("/")
            if not token:
                continue
            paths.append(f"{prefix}{token}")
    return paths


def test_required_layout_exists() -> None:
    missing = [path for path in REQUIRED_FILES if not (REPO_ROOT / path).is_file()]

    assert not missing, f"the handoff's required layout is incomplete: {missing}"


def test_a_makefile_or_the_wrapper_scripts_exist() -> None:
    for group in REQUIRED_ALTERNATIVE_GROUPS:
        present = [name for name in group if (REPO_ROOT / name).is_file()]
        assert present, f"none of {group} exists"

    # the wrappers are documented as runnable, so both are shipped here
    assert (REPO_ROOT / "Makefile").is_file()
    assert (REPO_ROOT / "up.sh").is_file() and (REPO_ROOT / "up.ps1").is_file()


def test_required_directories_exist() -> None:
    for directory in ("emulator/app", "emulator/tests", "scripts", "docs", "broker"):
        assert (REPO_ROOT / directory).is_dir(), f"{directory}/ is missing"


def test_readme_layout_lists_only_paths_that_exist() -> None:
    listed = layout_paths()

    assert listed, "the README layout block could not be parsed"
    missing = sorted({path for path in listed if not (REPO_ROOT / path).exists()})

    assert not missing, f"the README layout lists paths that do not exist: {missing}"


def test_readme_layout_covers_every_required_deliverable() -> None:
    listed = set(layout_paths())

    for path in REQUIRED_FILES:
        assert path in listed, f"{path} is required by the handoff but missing from the README layout"


def test_every_test_module_in_the_layout_exists() -> None:
    """Every test module quoted in the layout must be a real file."""

    quoted = [path for path in layout_paths() if path.endswith(".py") and "test" in Path(path).name]

    assert quoted, "the README layout block quotes no test modules"
    for path in quoted:
        assert (REPO_ROOT / path).is_file(), f"the layout lists test module {path}, which does not exist"
