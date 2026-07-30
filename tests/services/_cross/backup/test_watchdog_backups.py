from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from toolkit.core.config.config import Config
from toolkit.core.ops.backup_inventory import BackupInventory, BackupNodeState
from toolkit.core.ops.watchdog import Watchdog, WatchdogReport
from toolkit.core.watchdog.models import NOTIFY_COOLDOWN_INFRA_S, HealthIssue


def _watchdog(tmp_path: Path, monkeypatch, *, role: str = "infra") -> Watchdog:
    monkeypatch.setenv("HOMELAB_NODE", role)
    monkeypatch.setattr(Watchdog, "_discover_docker_labels", lambda self: None)
    monkeypatch.setattr(Watchdog, "_merge_service_metadata", lambda self: None)
    return Watchdog(tmp_path, Config(backups={"enabled": True}))


def test_backup_freshness_reports_stale_and_missing_nodes_as_critical(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory(
            (
                BackupNodeState("infra", "fresh", True, age_hours=2),
                BackupNodeState("media", "stale", False, age_hours=30),
                BackupNodeState("apps", "missing", False),
            )
        ),
    )

    issues = watchdog.check_backup_freshness()

    assert [(issue.service, issue.severity) for issue in issues] == [
        ("backup-media", "critical"),
        ("backup-apps", "critical"),
    ]
    assert all(issue.category == "backups" and not issue.auto_fixable for issue in issues)
    assert "30.0 hours" in issues[0].diagnosis


def test_backup_freshness_reports_repository_unavailability_as_infrastructure_issue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory((BackupNodeState("infra", "error", False),), "inventory unavailable"),
    )

    issues = watchdog.check_backup_freshness()

    assert len(issues) == 1
    assert issues[0].service == "backup-repository"
    assert issues[0].severity == "infra"


def test_backup_freshness_skips_non_infra_guest(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch, role="media")
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: (_ for _ in ()).throw(AssertionError("repository must not be queried")),
    )

    assert watchdog.check_backup_freshness() == []


def _write_drill(tmp_path: Path, *, ok: bool, checked_at: datetime) -> None:
    path = tmp_path / ".homelab-state" / "backup-drills" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "checked_at": checked_at.isoformat(),
                "ok": ok,
                "nodes": [{"role": "infra", "ok": ok, "artifact_count": 3, "error": "failed" if not ok else ""}],
                "errors": ["private failure"] if not ok else [],
            }
        ),
        encoding="utf-8",
    )


def test_backup_drill_health_reports_missing_failed_and_stale_evidence(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch)
    missing = watchdog.check_backup_restore_drill()
    assert [(issue.service, issue.severity) for issue in missing] == [("backup-restore-drill", "warning")]

    _write_drill(tmp_path, ok=False, checked_at=datetime.now(UTC))
    failed = watchdog.check_backup_restore_drill()
    assert [(issue.service, issue.severity) for issue in failed] == [("backup-restore-drill", "critical")]
    assert "private failure" not in failed[0].message

    _write_drill(tmp_path, ok=True, checked_at=datetime.now(UTC) - timedelta(days=9))
    stale = watchdog.check_backup_restore_drill()
    assert [(issue.service, issue.severity) for issue in stale] == [("backup-restore-drill", "critical")]


def test_backup_drill_health_skips_non_infra_guest(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch, role="apps")
    assert watchdog.check_backup_restore_drill() == []


def test_watchdog_metrics_export_backup_health_and_age(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory(
            (
                BackupNodeState("infra", "fresh", True, age_hours=2.5),
                BackupNodeState("media", "stale", False, age_hours=30),
            )
        ),
    )

    _write_drill(tmp_path, ok=True, checked_at=datetime.now(UTC))
    watchdog.check_backup_freshness()
    watchdog.check_backup_restore_drill()
    metrics = watchdog.prometheus_metrics(WatchdogReport())

    assert 'watchdog_backup_healthy{node="infra"} 1' in metrics
    assert 'watchdog_backup_healthy{node="media"} 0' in metrics
    assert 'watchdog_backup_age_hours{node="infra"} 2.50' in metrics
    assert "watchdog_backup_restore_drill_healthy 1" in metrics


def test_backup_alert_remains_suppressed_after_one_hour(tmp_path: Path, monkeypatch) -> None:
    watchdog = _watchdog(tmp_path, monkeypatch)
    issue = HealthIssue("backup-media", "backups", "critical", "Latest snapshot is stale")
    report = WatchdogReport(issues=[issue])
    with patch.object(watchdog, "_send_ntfy", return_value=True):
        watchdog.notify(report)
    watchdog._reset_notify_state_for_test(now=time.time() - 3_600)

    next_watchdog = _watchdog(tmp_path, monkeypatch)
    with patch.object(next_watchdog, "_send_ntfy", return_value=True) as send:
        next_watchdog.notify(report)

    assert next_watchdog._cooldown_for(issue) == NOTIFY_COOLDOWN_INFRA_S
    assert send.call_count == 0
