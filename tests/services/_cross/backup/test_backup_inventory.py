from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from toolkit.core.config.config import Config
from toolkit.core.ops.backup_inventory import parse_snapshot_inventory, read_backup_inventory


def test_snapshot_inventory_reports_each_enabled_node_independently() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    body = json.dumps(
        [
            {
                "id": f"snapshot-{role}",
                "startTime": (now - timedelta(hours=index + 1)).isoformat(),
                "source": {"host": f"homelab-{role}", "path": "/source"},
                "rootEntry": {"obj": f"k{'a' * 31}{index}"},
                "stats": {"totalSize": 1024 * (index + 1)},
            }
            for index, role in enumerate(("infra", "media", "apps"))
        ]
    )

    inventory = parse_snapshot_inventory(body, Config(), now=now)

    assert [node.role for node in inventory.nodes] == ["infra", "media", "apps"]
    assert all(node.ok for node in inventory.nodes)
    assert inventory.nodes[1].age_hours == 2.0
    assert inventory.nodes[2].size_bytes == 3072
    assert inventory.nodes[0].root_object_id == f"k{'a' * 31}0"


def test_snapshot_inventory_marks_missing_and_stale_nodes() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    body = json.dumps(
        [
            {
                "id": "old-infra",
                "startTime": (now - timedelta(hours=30)).isoformat(),
                "source": {"host": "homelab-infra", "path": "/source"},
            }
        ]
    )

    inventory = parse_snapshot_inventory(body, Config(), now=now)

    assert not inventory.nodes[0].ok
    assert inventory.nodes[0].status == "stale"
    assert inventory.nodes[1].status == "missing"
    assert inventory.nodes[2].status == "missing"


def test_snapshot_inventory_rejects_newest_incomplete_snapshot() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    body = json.dumps(
        [
            {
                "id": "complete-infra",
                "startTime": (now - timedelta(hours=2)).isoformat(),
                "source": {"host": "homelab-infra", "path": "/source"},
                "rootEntry": {"obj": f"k{'a' * 32}", "summ": {"numFailed": 0}},
                "stats": {"totalSize": 1024, "errorCount": 0},
            },
            {
                "id": "incomplete-infra",
                "startTime": (now - timedelta(hours=1)).isoformat(),
                "source": {"host": "homelab-infra", "path": "/source"},
                "rootEntry": {"obj": f"k{'b' * 32}", "summ": {"numFailed": 1}},
                "stats": {"totalSize": 2048, "errorCount": 1},
            },
        ]
    )

    inventory = parse_snapshot_inventory(body, Config(), now=now)

    assert inventory.nodes[0].status == "error"
    assert not inventory.nodes[0].ok
    assert inventory.nodes[0].snapshot_id == "incomplete-infra"


def test_snapshot_inventory_fails_closed_on_invalid_or_unbounded_input() -> None:
    for body in ("not-json", json.dumps({"unexpected": True}), "[" + "{} ," * 1001 + "{}]"):
        inventory = parse_snapshot_inventory(body, Config())
        assert inventory.error
        assert all(not node.ok for node in inventory.nodes)


def test_remote_inventory_uses_infra_transport_and_bounded_query(tmp_path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    recent = datetime.now(UTC).isoformat()
    body = json.dumps([{"id": "infra", "startTime": recent, "source": {"host": "homelab-infra"}}])
    commands: list[str] = []

    def fake_ssh(_cfg, _ip, command, **_kwargs):
        commands.append(command)
        return 0, body, ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    inventory = read_backup_inventory(cfg, tmp_path)

    assert commands
    assert "--all" in commands[0]
    assert "--max-results=50" in commands[0]
    assert inventory.nodes[0].ok


def test_inventory_transport_error_does_not_expose_command_output(tmp_path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (1, "", "repository password=do-not-expose"),
    )

    inventory = read_backup_inventory(cfg, tmp_path)

    assert inventory.error == "snapshot inventory is unavailable on the backup node"
    assert "do-not-expose" not in inventory.error
    assert all(node.status == "error" for node in inventory.nodes)


def test_snapshot_inventory_only_projects_physical_nodes_in_monolithic_mode() -> None:
    cfg = Config(proxmox={"provision_machines": False})
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    body = json.dumps(
        [
            {
                "id": role,
                "startTime": (now - timedelta(hours=1)).isoformat(),
                "source": {"host": f"homelab-{role}"},
            }
            for role in ("infra", "media", "apps")
        ]
    )

    inventory = parse_snapshot_inventory(body, cfg, now=now)

    assert [node.role for node in inventory.nodes] == ["infra"]
    assert inventory.ok


def test_monolithic_inventory_reads_the_local_kopia_container(tmp_path, monkeypatch) -> None:
    cfg = Config(proxmox={"provision_machines": False}, backups={"enabled": True})
    recent = datetime.now(UTC).isoformat()
    body = json.dumps([{"id": "infra", "startTime": recent, "source": {"host": "homelab-infra"}}])
    calls: list[tuple[str, list[str]]] = []

    def fake_exec(service, command, **_kwargs):
        calls.append((service, command))
        return 0, body

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SSH must not be used")),
    )

    inventory = read_backup_inventory(cfg, tmp_path)

    assert inventory.ok
    assert calls[0][0] == "kopia"
