"""Guard the separation between framework contracts and service tests."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_ROOT = REPO_ROOT / "tests" / "framework"
SERVICE_OWNED_TASK_PATH = re.compile(r"toolkit/services/[a-z0-9][a-z0-9-]*/ansible/")


def test_framework_collection_excludes_service_owned_tests() -> None:
    """Core collection must never silently expand into ``tests/services``."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            str(FRAMEWORK_ROOT),
            "--ignore",
            str(Path(__file__).resolve()),
            "--disable-warnings",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    node_ids = [line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    assert node_ids, result.stdout
    assert all(node_id.startswith("tests/framework/") for node_id in node_ids)
    assert not any("tests/services" in node_id for node_id in node_ids)


def test_framework_sources_do_not_import_service_implementations() -> None:
    """Prevent service behavior tests from quietly returning to the framework tree."""
    violations: list[str] = []
    for path in FRAMEWORK_ROOT.rglob("*.py"):
        if path == Path(__file__).resolve():
            continue
        source = path.read_text(encoding="utf-8")
        if SERVICE_OWNED_TASK_PATH.search(source):
            violations.append(f"{path.relative_to(REPO_ROOT)}: service-owned Ansible path")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif module:
                modules = [module]
            else:
                modules = []
            for imported in modules:
                if imported == "toolkit.services" or imported.startswith("toolkit.services."):
                    violations.append(f"{path.relative_to(REPO_ROOT)}: {imported}")

    assert not violations, "service implementation imports belong under tests/services/:\n" + "\n".join(violations)
