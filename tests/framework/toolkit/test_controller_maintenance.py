from __future__ import annotations

from pathlib import Path

from toolkit.controller.contracts import BackupDrillOperation, MaintenanceOperation
from toolkit.controller.operations import _backup_drill_handler, _maintenance_handler
from toolkit.core.config.config import Config, save_config
from toolkit.core.ops.backup_restore_drill import BackupDrillNodeResult, BackupRestoreDrillResult
from toolkit.core.ops.cluster_maintenance import ClusterMaintenanceResult, NodeMaintenanceState


class _Context:
    actor = "owner"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def check_cancelled(self) -> None:
        return None

    def log(self, message: str, payload: dict | None = None, **_kwargs) -> None:
        self.events.append((message, payload or {}))


def test_controller_maintenance_handler_runs_cluster_orchestrator(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(), tmp_path)
    context = _Context()
    calls: list[str] = []

    def fake_run(_cfg, _root, *, actor, on_log, check_cancelled):
        calls.append(actor)
        check_cancelled()
        on_log("Infra completed", {"role": "infra"})
        return ClusterMaintenanceResult(
            nodes=[NodeMaintenanceState("infra", True, None)],
            actions=["infra maintenance completed"],
        )

    monkeypatch.setattr("toolkit.core.ops.cluster_maintenance.run_cluster_maintenance", fake_run)

    result = _maintenance_handler(tmp_path)(context, MaintenanceOperation())

    assert calls == ["owner"]
    assert result == {"ok": True, "node_count": 1, "action_count": 1}
    assert [message for message, _payload in context.events] == ["Infra completed", "Maintenance completed"]


def test_controller_backup_drill_handler_reports_verified_content(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(backups={"enabled": True}), tmp_path)
    context = _Context()
    monkeypatch.setattr(
        "toolkit.core.ops.backup_restore_drill.run_backup_restore_drill",
        lambda _cfg, _root, *, actor: BackupRestoreDrillResult(
            True,
            (
                BackupDrillNodeResult("infra", True, 3),
                BackupDrillNodeResult("apps", True, 2),
            ),
        ),
    )

    result = _backup_drill_handler(tmp_path)(context, BackupDrillOperation())

    assert result == {"ok": True, "node_count": 2, "artifact_count": 5}
    assert context.events[-1] == (
        "Backup content drill completed",
        {"ok": True, "node_count": 2, "artifact_count": 5, "error_count": 0},
    )
