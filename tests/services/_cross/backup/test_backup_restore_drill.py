from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli.maintenance_cmd import maintenance
from toolkit.core.config.config import Config
from toolkit.core.deploy.operation_lease import LeaseBusyError
from toolkit.core.ops.backup_inventory import BackupInventory, BackupNodeState
from toolkit.core.ops.backup_restore_drill import (
    BackupDrillEvidence,
    BackupDrillNodeResult,
    BackupRestoreDrillResult,
    read_backup_drill_evidence,
    run_backup_restore_drill,
)


def _inventory() -> BackupInventory:
    return BackupInventory(
        (
            BackupNodeState("infra", "fresh", True, root_object_id="k" + "a" * 32),
            BackupNodeState("media", "fresh", True, root_object_id="k" + "b" * 32),
            BackupNodeState("apps", "fresh", True, root_object_id="k" + "c" * 32),
        )
    )


def test_backup_restore_drill_restores_bounded_expected_artifacts_on_infra(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    commands: list[str] = []
    monkeypatch.setattr("toolkit.core.ops.backup_inventory.read_backup_inventory", lambda *_args: _inventory())

    def fake_ssh(_cfg, _ip, command, **_kwargs):
        commands.append(command)
        expected = 4 if "backup-dumps/infra" in command else (3 if "backup-dumps/apps" in command else 0)
        return 0, f"ARTIFACT_COUNT={expected}\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    result = run_backup_restore_drill(cfg, tmp_path, actor="test")

    assert result.ok
    assert [(node.role, node.artifact_count) for node in result.nodes] == [
        ("infra", 4),
        ("media", 0),
        ("apps", 3),
    ]
    assert len(commands) == 3
    assert "kaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/backup-dumps/infra" in commands[0]
    assert "postgres.sql.gz" in commands[0]
    assert "komodo-mongo.archive.gz" in commands[0]
    assert "roundcube.sqlite.gz" in commands[0]
    assert "headscale.sqlite.gz" in commands[0]
    assert "kbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/config.yaml" in commands[1]
    assert "kc" + "c" * 31 + "/backup-dumps/apps" in commands[2]
    evidence = json.loads((tmp_path / ".homelab-state" / "backup-drills" / "latest.json").read_text())
    assert evidence["ok"] is True
    assert [node["role"] for node in evidence["nodes"]] == ["infra", "media", "apps"]
    assert not (tmp_path / ".homelab-state" / "checkpoints" / "latest.json").exists()


def test_backup_restore_drill_fails_without_restorable_root_object(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True}, proxmox={"provision_machines": False})
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory((BackupNodeState("infra", "fresh", True),)),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.automation.docker_exec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("restore must not run")),
    )

    result = run_backup_restore_drill(cfg, tmp_path, actor="test")

    assert not result.ok
    assert result.errors == ("infra snapshot has no restorable root object",)


def test_monolithic_backup_restore_drill_executes_in_local_kopia_container(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True}, proxmox={"provision_machines": False})
    inventory = BackupInventory((BackupNodeState("infra", "fresh", True, root_object_id="k" + "a" * 32),))
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("toolkit.core.ops.backup_inventory.read_backup_inventory", lambda *_args: inventory)

    def fake_exec(service, command, **_kwargs):
        calls.append((service, command))
        return 0, "ARTIFACT_COUNT=4"

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)

    result = run_backup_restore_drill(cfg, tmp_path)

    assert result.ok
    assert calls[0][0] == "kopia"
    assert calls[0][1][:2] == ["sh", "-ec"]


def test_backup_restore_drill_refuses_when_backups_are_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: (_ for _ in ()).throw(AssertionError("inventory must not be queried")),
    )

    result = run_backup_restore_drill(Config(backups={"enabled": False}), tmp_path)

    assert not result.ok
    assert result.errors == ("backups are disabled",)
    assert not (tmp_path / ".homelab-state" / "backup-drills" / "latest.json").exists()


def test_backup_restore_drill_defers_without_replacing_evidence_when_operation_is_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence_path = tmp_path / ".homelab-state" / "backup-drills" / "latest.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"existing": true}', encoding="utf-8")
    monkeypatch.setattr(
        "toolkit.core.ops.backup_restore_drill.OperationLease.acquire",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LeaseBusyError(tmp_path / "operation.lock")),
    )
    with patch("toolkit.core.ops.notifications.send_ntfy") as notify:
        result = run_backup_restore_drill(Config(backups={"enabled": True}), tmp_path)

    assert not result.ok
    assert result.deferred
    assert evidence_path.read_text(encoding="utf-8") == '{"existing": true}'
    notify.assert_not_called()


def test_backup_drill_evidence_reader_is_strict_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / ".homelab-state" / "backup-drills" / "latest.json"
    path.parent.mkdir(parents=True)
    checked_at = datetime(2026, 7, 12, 5, 30, tzinfo=UTC)
    path.write_text(
        json.dumps(
            {
                "checked_at": checked_at.isoformat(),
                "ok": True,
                "nodes": [
                    {"role": "infra", "ok": True, "artifact_count": 3, "error": ""},
                    {"role": "apps", "ok": True, "artifact_count": 2, "error": ""},
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    assert read_backup_drill_evidence(tmp_path) == BackupDrillEvidence(
        checked_at=checked_at,
        ok=True,
        node_count=2,
        artifact_count=5,
        error_count=0,
    )

    path.write_text("{}", encoding="utf-8")
    assert read_backup_drill_evidence(tmp_path) is None


def test_backup_restore_drill_cli_reports_per_node_progress(tmp_path: Path) -> None:
    cfg = Config(backups={"enabled": True})
    completed = BackupRestoreDrillResult(
        True,
        (
            BackupDrillNodeResult("infra", True, 3),
            BackupDrillNodeResult("media", True, 0),
            BackupDrillNodeResult("apps", True, 2),
        ),
    )
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, cfg)),
        patch("toolkit.core.ops.backup_restore_drill.run_backup_restore_drill", return_value=completed),
    ):
        result = CliRunner().invoke(maintenance, ["backup-drill"])

    assert result.exit_code == 0, result.output
    assert "infra: verified 3 logical artifact(s)" in result.output
    assert "media: verified snapshot content" in result.output
    assert "apps: verified 2 logical artifact(s)" in result.output
