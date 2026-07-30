from __future__ import annotations

import ast
from pathlib import Path


def test_production_code_does_not_use_optimization_sensitive_assertions() -> None:
    root = Path(__file__).resolve().parents[4]
    violations: list[str] = []
    for source in sorted((root / "toolkit").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        violations.extend(
            f"{source.relative_to(root)}:{node.lineno}" for node in ast.walk(tree) if isinstance(node, ast.Assert)
        )

    assert not violations, "production assertions disappear under python -O:\n" + "\n".join(violations)


def test_core_automation_helpers_are_service_agnostic() -> None:
    root = Path(__file__).resolve().parents[4]
    source = (root / "toolkit/core/ops/automation.py").read_text(encoding="utf-8").casefold()

    for service in ("grafana", "jellyfin", "music-sync", "music_sync", "nextcloud"):
        assert service not in source, f"{service} automation belongs in its service plugin"
