from __future__ import annotations

import json
from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.core.ops.backup_inventory import BackupInventory, BackupNodeState
from toolkit.core.ops.cluster_maintenance import run_cluster_maintenance
from toolkit.core.state.audit_log import read_audit


def test_cluster_maintenance_runs_each_provisioned_role_and_verifies_backups(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = Config(backups={"enabled": True})
    commands: list[tuple[str, str]] = []

    def fake_ssh(_cfg, ip, command, **_kwargs):
        commands.append((ip, command))
        return 0, "completed", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: type("Inventory", (), {"ok": True, "error": "", "nodes": ()})(),
    )

    result = run_cluster_maintenance(cfg, tmp_path)

    assert result.ok
    assert [node.role for node in result.nodes] == ["infra", "media", "apps"]
    assert all(node.maintenance_ok and node.snapshot_ok for node in result.nodes)
    for role in ("infra", "media", "apps"):
        role_commands = [command for ip, command in commands if ip == cfg.node_ip(role)]
        assert any(f"maintenance run --node {role} --no-notify" in command for command in role_commands)
        assert any(f"maintenance snapshot --node {role}" in command for command in role_commands)
    state = json.loads((tmp_path / "data" / "maintenance" / "last-run.json").read_text())
    assert state["ok"] is True
    assert [node["role"] for node in state["nodes"]] == ["infra", "media", "apps"]
    assert read_audit(tmp_path, action="maintenance")[-1]["ok"] is True


def test_cluster_maintenance_skips_snapshots_when_backups_are_disabled(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": False})
    commands: list[str] = []

    def fake_ssh(_cfg, _ip, command, **_kwargs):
        commands.append(command)
        return 0, "completed", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    result = run_cluster_maintenance(cfg, tmp_path)

    assert result.ok
    assert all(node.snapshot_ok is None for node in result.nodes)
    assert not any("maintenance snapshot" in command for command in commands)


def test_cluster_maintenance_fails_closed_without_persisting_remote_output(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": False})

    def fake_ssh(_cfg, ip, _command, **_kwargs):
        if ip == cfg.node_ip("media"):
            return 1, "", "password=do-not-persist"
        return 0, "completed", ""

    notifications: list[str] = []
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    monkeypatch.setattr(
        "toolkit.core.ops.notifications.send_ntfy",
        lambda message, *_args, **_kwargs: notifications.append(message),
    )

    result = run_cluster_maintenance(cfg, tmp_path)

    assert not result.ok
    assert result.nodes[1].maintenance_ok is False
    assert result.errors == ["media maintenance failed"]
    assert notifications
    state = (tmp_path / "data" / "maintenance" / "last-run.json").read_text()
    assert "do-not-persist" not in state


def test_cluster_maintenance_collapses_monolithic_deployments_to_infra(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(proxmox={"provision_machines": False}, backups={"enabled": True})
    maintained: list[str] = []
    snapshots: list[str] = []
    monkeypatch.setattr(
        "toolkit.core.ops.maintenance.run_maintenance",
        lambda _root, *, vm, **_kwargs: maintained.append(vm) or type("Result", (), {"ok": True})(),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.backups.run_node_snapshot",
        lambda _root, role, **_kwargs: snapshots.append(role) or type("Result", (), {"ok": True})(),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: type("Inventory", (), {"ok": True, "error": "", "nodes": ()})(),
    )

    result = run_cluster_maintenance(cfg, tmp_path)

    assert maintained == ["infra"]
    assert snapshots == ["infra"]
    assert [node.role for node in result.nodes] == ["infra"]


def test_cluster_maintenance_marks_node_failed_when_snapshot_verification_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = Config(backups={"enabled": True})
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (0, "completed", ""),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.backup_inventory.read_backup_inventory",
        lambda *_args: BackupInventory(
            (
                BackupNodeState("infra", "fresh", True),
                BackupNodeState("media", "stale", False),
                BackupNodeState("apps", "fresh", True),
            )
        ),
    )

    result = run_cluster_maintenance(cfg, tmp_path)

    assert not result.ok
    assert result.nodes[1].snapshot_ok is False
    assert result.errors == ["media backup verification is stale"]
