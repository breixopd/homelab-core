"""Shared update-discovery script runners and report validation.

The CLI and controller use these helpers to run version discovery. This module
owns that process boundary so callers only handle orchestration and presentation.

Discovery failures are explicit so callers cannot misreport a failed scan as
an up-to-date framework.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

UPDATES_CACHE = "generated/updates-cache.json"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


class UpdateCheckError(RuntimeError):
    """Version discovery did not produce a trustworthy report."""


def _report(stdout: str, *, label: str) -> list[dict]:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise UpdateCheckError(f"{label} returned invalid JSON") from exc
    if not isinstance(report, list) or any(not isinstance(item, dict) for item in report):
        raise UpdateCheckError(f"{label} returned an invalid report")
    return report


def bump_script() -> Path:
    """Path to ``scripts/bump-versions.py`` at the repo root."""
    return _SCRIPTS_DIR / "bump-versions.py"


def framework_check_script() -> Path:
    """Path to the source-controlled framework dependency checker."""
    return _SCRIPTS_DIR / "check-framework-updates.py"


def run_check(root: Path, *, refresh: bool = False, compose_file: Path | None = None) -> list[dict]:
    """Run ``bump-versions.py`` and return the parsed JSON report.

    With ``refresh=True`` (web UI), passes ``--refresh`` to bypass the cache.
    With ``refresh=False`` (CLI), passes ``--cache`` to populate it.
    Raises :class:`UpdateCheckError` when discovery cannot be trusted.
    """
    flag = "--refresh" if refresh else "--cache"
    try:
        command = [sys.executable, str(bump_script()), "--json", flag]
        command.extend(["--cache-file", str((root / UPDATES_CACHE).resolve())])
        if compose_file is not None:
            command.extend(["--compose-file", str(compose_file.resolve())])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "scanner exited unsuccessfully").strip()[:200]
            raise UpdateCheckError(f"image update scan failed: {detail}")
        if not result.stdout.strip():
            raise UpdateCheckError("image update scan returned no report")
        return _report(result.stdout, label="image update scan")
    except subprocess.TimeoutExpired as exc:
        raise UpdateCheckError("image update scan timed out") from exc
    except OSError as exc:
        raise UpdateCheckError("image update scan could not start") from exc


def run_framework_check(root: Path) -> list[dict]:
    """Check source-controlled framework dependencies and return the report.

    Raises :class:`UpdateCheckError` when discovery cannot be trusted.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(framework_check_script()), "--json", "--root", str(root.resolve())],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=root,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "scanner exited unsuccessfully").strip()[:200]
            raise UpdateCheckError(f"framework update scan failed: {detail}")
        if not result.stdout.strip():
            raise UpdateCheckError("framework update scan returned no report")
        return _report(result.stdout, label="framework update scan")
    except subprocess.TimeoutExpired as exc:
        raise UpdateCheckError("framework update scan timed out") from exc
    except OSError as exc:
        raise UpdateCheckError("framework update scan could not start") from exc
