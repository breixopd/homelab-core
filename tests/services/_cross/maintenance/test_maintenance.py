from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.machines import renamed_default_machines
from toolkit.core.config.config import Config, save_config
from toolkit.core.ops.maintenance import (
    check_os_patch_state,
    prometheus_metrics,
    run_docker_cleanup,
    run_maintenance,
    trim_homelab_logs,
)
from toolkit.core.state.audit_log import read_audit


def test_trim_homelab_logs_removes_old(tmp_path: Path):
    old = tmp_path / "deploy.log"
    old.write_text("x")
    old_mtime = 0.0
    import os

    os.utime(old, (old_mtime, old_mtime))
    logs = trim_homelab_logs(tmp_path, max_age_days=7)
    assert not old.exists()
    assert logs


def test_run_maintenance_records_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance.run_docker_cleanup",
        lambda **_: ["Docker dangling images: ok"],
    )
    monkeypatch.setattr("toolkit.core.ops.maintenance.vacuum_journal", lambda **_: ["Journal: ok"])
    monkeypatch.setattr("toolkit.core.ops.maintenance.trim_homelab_logs", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance_tasks.scan_image_updates",
        lambda *a, **k: [],
    )

    result = run_maintenance(tmp_path, vm="media")
    assert result.ok
    state = tmp_path / "data" / "maintenance" / "last-run.json"
    assert state.is_file()
    assert json.loads(state.read_text())["actions"] == result.actions
    audit = read_audit(tmp_path, action="maintenance")
    assert audit[-1]["vm"] == "media"
    assert audit[-1]["ok"] is True


def test_run_maintenance_uses_saved_policy(tmp_path: Path, monkeypatch) -> None:
    save_config(
        Config(
            maintenance={
                "image_update_scan": False,
                "cert_warning_days": 30,
            }
        ),
        tmp_path,
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr("toolkit.core.ops.maintenance.run_docker_cleanup", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.vacuum_journal", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.trim_homelab_logs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance_tasks.scan_image_updates",
        lambda *, root, cfg: (
            observed.update(
                image_update_scan=cfg.image_update_scan,
                cert_warning_days=cfg.cert_warning_days,
            )
            or []
        ),
    )

    result = run_maintenance(tmp_path, notify_on_attention=False)

    assert result.ok
    assert observed == {"image_update_scan": False, "cert_warning_days": 30}


def test_run_maintenance_resolves_custom_control_machine(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(machines=renamed_default_machines()), tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setattr("toolkit.core.ops.maintenance.run_docker_cleanup", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.vacuum_journal", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.trim_homelab_logs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance_tasks.scan_image_updates",
        lambda **_: observed.update(scanned=True) or [],
    )

    result = run_maintenance(tmp_path, vm="core", notify_on_attention=False)

    assert result.ok
    assert observed["scanned"] is True
    assert json.loads((tmp_path / "data" / "maintenance" / "last-run.json").read_text())["vm"] == "core"


def test_run_maintenance_promotes_action_failures_and_notifies(tmp_path: Path, monkeypatch):
    sent: list[dict[str, str]] = []

    monkeypatch.setattr(
        "toolkit.core.ops.maintenance.run_docker_cleanup",
        lambda **_: ["Docker old unused images failed: disk full"],
    )
    monkeypatch.setattr("toolkit.core.ops.maintenance.vacuum_journal", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.trim_homelab_logs", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.config.service_metadata._load_all_services", lambda: {})
    monkeypatch.setattr("toolkit.core.ops.maintenance_tasks.scan_image_updates", lambda *a, **k: [])
    monkeypatch.setattr(
        "toolkit.core.ops.notifications.send_ntfy",
        lambda message, title, priority, root, **_k: sent.append(
            {"message": message, "title": title, "priority": priority}
        ),
    )

    result = run_maintenance(tmp_path, vm="media")

    assert not result.ok
    assert result.errors == ["Docker old unused images failed: disk full"]
    assert sent
    assert sent[0]["priority"] == "high"
    assert "Docker old unused images failed" in sent[0]["message"]


def test_run_maintenance_notifies_notices_without_failing(tmp_path: Path, monkeypatch):
    sent: list[dict[str, str]] = []

    monkeypatch.setattr("toolkit.core.ops.maintenance.run_docker_cleanup", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.vacuum_journal", lambda **_: [])
    monkeypatch.setattr("toolkit.core.ops.maintenance.trim_homelab_logs", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.config.service_metadata._load_all_services", lambda: {})
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance_tasks.scan_image_updates",
        lambda *a, **k: [{"service": "ntfy", "current": "v1", "latest": "v2"}],
    )
    monkeypatch.setattr(
        "toolkit.core.ops.notifications.send_ntfy",
        lambda message, title, priority, root, **_k: sent.append(
            {"message": message, "title": title, "priority": priority}
        ),
    )

    result = run_maintenance(tmp_path, notify_on_attention=True)

    assert result.ok
    assert sent
    assert sent[0]["priority"] == "default"
    assert "Image updates available" in sent[0]["message"]


def test_run_docker_cleanup_does_not_use_volume_prune(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return 0, "Total reclaimed space: 0B", ""

    monkeypatch.setattr("toolkit.core.ops.maintenance._run", fake_run)
    run_docker_cleanup()
    assert all("volume" not in " ".join(c) for c in calls)


def test_check_os_patch_state_reports_pending_reboot(tmp_path: Path, monkeypatch) -> None:
    reboot_flag = tmp_path / "reboot-required"
    reboot_flag.write_text("linux-image\n", encoding="utf-8")
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance._run",
        lambda *_args, **_kwargs: (0, "LoadState=loaded\nActiveState=inactive\nResult=success", ""),
    )

    state = check_os_patch_state(reboot_flag=reboot_flag, systemd_runtime=tmp_path)

    assert state.reboot_required is True
    assert state.updates_healthy is True
    assert state.notices == ["WARN: operating-system updates require a reboot"]
    assert state.failures == []


def test_check_os_patch_state_reports_failed_unattended_upgrade(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance._run",
        lambda *_args, **_kwargs: (0, "LoadState=loaded\nActiveState=failed\nResult=exit-code", ""),
    )

    state = check_os_patch_state(reboot_flag=tmp_path / "missing", systemd_runtime=tmp_path)

    assert state.updates_healthy is False
    assert state.failures == ["CRITICAL: unattended operating-system upgrades failed (exit-code)"]


def test_check_os_patch_state_skips_non_systemd_environments(tmp_path: Path) -> None:
    state = check_os_patch_state(reboot_flag=tmp_path / "missing", systemd_runtime=tmp_path / "missing")

    assert state.updates_healthy is None
    assert state.notices == []
    assert state.failures == []


def test_maintenance_metrics_expose_os_patch_state(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "data" / "maintenance" / "last-run.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "timestamp": 123,
                "ok": True,
                "reboot_required": True,
                "os_updates_healthy": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("toolkit.core.ops.maintenance._run", lambda *_args, **_kwargs: (0, "Use%\n42%", ""))

    metrics = prometheus_metrics(tmp_path)

    assert "homelab_os_reboot_required 1" in metrics
    assert "homelab_os_updates_healthy 0" in metrics
