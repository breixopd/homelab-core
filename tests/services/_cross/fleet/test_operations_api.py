from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from toolkit.controller.operations_api import read_operations_view
from toolkit.core.config.config import Config, ExternalHost, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.ops.backup_inventory import BackupInventory, BackupNodeState


def test_operations_view_projects_bounded_maintenance_backup_and_fleet_state(tmp_path: Path, monkeypatch) -> None:
    host = ExternalHost(
        name="nas-01",
        ip="192.0.2.40",
        kind="fleet",
        reconciled=True,
        last_reconcile_at="2026-07-11T01:00:00+00:00",
        services=["monitoring-agent", "media-cache", "backup-storage"],
        applied_services=["monitoring-agent", "media-cache", "backup-storage"],
        integrations={
            "media-cache": {"path": "/srv/media"},
            "backup-storage": {"path": "/srv/backups"},
        },
    )
    cfg = Config(
        domain="example.test",
        backups={"enabled": True, "target": "remote", "storage_host": "nas-01"},
        external_hosts=[host],
        proxmox={"provision_machines": False},
        services={
            "media": False,
            "cloud": False,
            "notifications": False,
            "email": False,
            "security": False,
        },
    )
    save_config(cfg, config_path(tmp_path))
    state = tmp_path / "data" / "maintenance" / "last-run.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"timestamp": 1_786_400_000, "ok": False, "actions": ["one", "two"], "errors": ["private"]}),
        encoding="utf-8",
    )
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    dump_dir.mkdir(parents=True)
    with gzip.open(dump_dir / "pre-deploy-20260711-010203.sql.gz", "wb") as stream:
        stream.write(b"select 1;")
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory((BackupNodeState("infra", "fresh", True, snapshot_count=4),)),
    )
    drill = tmp_path / ".homelab-state" / "backup-drills" / "latest.json"
    drill.parent.mkdir(parents=True)
    drill.write_text(
        json.dumps(
            {
                "checked_at": datetime(2026, 7, 12, 5, 30, tzinfo=UTC).isoformat(),
                "ok": True,
                "nodes": [{"role": "infra", "ok": True, "artifact_count": 3, "error": ""}],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    view = read_operations_view(tmp_path)

    assert view.backups.enabled is True
    assert view.backups.target == "remote"
    assert view.backups.storage_host == "nas-01"
    assert view.backups.ok is True
    assert view.backups.nodes[0].role == "infra"
    assert view.backups.nodes[0].snapshot_count == 4
    assert view.backups.drill.ok is True
    assert view.backups.drill.artifact_count == 3
    assert view.backups.drill.last_run_at == datetime(2026, 7, 12, 5, 30, tzinfo=UTC)
    assert view.maintenance.ok is False
    assert view.maintenance.schedule_label == "Daily at 03:00"
    assert view.maintenance.action_count == 2
    assert view.maintenance.error_count == 1
    assert len(view.dumps) == 1
    assert view.dumps[0].dump_id.startswith("dmp_")
    assert view.dumps[0].name == "pre-deploy-20260711-010203.sql.gz"
    assert view.hosts.hosts[0].name == "nas-01"
    assert view.hosts.hosts[0].services == ["monitoring-agent", "media-cache", "backup-storage"]
    assert "private" not in view.model_dump_json()
    assert view.updates.available is True
    assert view.updates.candidates == []


def test_operations_view_fails_closed_on_invalid_maintenance_state(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    state = tmp_path / "data" / "maintenance" / "last-run.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    view = read_operations_view(tmp_path)

    assert view.maintenance.last_run_at is None
    assert view.maintenance.ok is None
