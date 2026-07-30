"""Unit tests for toolkit.core.identity.ldap_guest_sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.identity.ldap_guest_sync import LdapSyncResult, sync_ldap_clients

_PLAYBOOK_REL = "automation/ansible/playbooks/sync-ldap-clients.yml"
_INVENTORY_REL = "automation/ansible/inventory/hosts.yml"


def _write_playbook(root: Path) -> Path:
    playbook = root / _PLAYBOOK_REL
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    return playbook


def _write_inventory(root: Path) -> Path:
    inv = root / _INVENTORY_REL
    inv.parent.mkdir(parents=True, exist_ok=True)
    inv.write_text("all:\n  hosts: {}\n")
    return inv


def _save_multi_vm_config(root: Path) -> Config:
    # Default Config enables management + media + cloud => multiple VMs => is_multi_vm True.
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))
    return cfg


def _save_single_vm_config(root: Path) -> Config:
    # Disable every optional service so only "infra" remains => is_multi_vm False.
    from toolkit.core.config.config import ServicesConfig

    cfg = Config(
        domain="example.com",
        email="admin@example.com",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    save_config(cfg, config_path(root))
    return cfg


def test_missing_playbook_returns_failure(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_multi_vm_config(root)
    # No playbook file written.

    result = sync_ldap_clients(root)

    assert result == LdapSyncResult(False, "sync-ldap-clients.yml missing", [])
    assert not result.ok
    assert "missing" in result.message


def test_single_host_mode_short_circuits_success(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _save_single_vm_config(root)

    # The module reads cfg.fleet.nodes (a list) to decide if any fleet
    # targets exist. Use a config-like mock so the short-circuit branch is
    # exercised without depending on the on-disk FleetConfig shape.
    mock_cfg = MagicMock()
    mock_cfg.is_multi_node = False
    mock_cfg.fleet.nodes = []

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.load_config",
            return_value=mock_cfg,
        ),
        patch("toolkit.core.identity.ldap_guest_sync.run_playbook_sync") as mock_run,
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root") as mock_sync,
    ):
        result = sync_ldap_clients(root)

    assert result.ok is True
    assert "single-host" in result.message
    mock_run.assert_not_called()
    mock_sync.assert_not_called()


def test_limit_propagated_to_runner(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _write_inventory(root)
    _save_multi_vm_config(root)

    captured: dict = {}

    def fake_run(root_arg, playbook_arg, *, inventory=None, limit=None, on_log=None):
        captured["root"] = root_arg
        captured["playbook"] = playbook_arg
        captured["inventory"] = inventory
        captured["limit"] = limit
        captured["on_log"] = on_log
        return MagicMock(ok=True, returncode=0)

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.run_playbook_sync",
            side_effect=fake_run,
        ) as mock_run,
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root") as mock_sync,
    ):
        result = sync_ldap_clients(root, limit="apps")

    assert result.ok is True
    assert "synced" in result.message
    assert captured["limit"] == "apps"
    assert captured["playbook"] == root / _PLAYBOOK_REL
    assert captured["inventory"] == root / _INVENTORY_REL
    mock_run.assert_called_once()
    mock_sync.assert_called_once_with(root)


def test_ansible_failure_returns_failure_result(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _write_inventory(root)
    _save_multi_vm_config(root)

    failure = MagicMock(ok=False, returncode=2)

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.run_playbook_sync",
            return_value=failure,
        ),
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root"),
    ):
        result = sync_ldap_clients(root, limit="infra")

    assert result.ok is False
    assert "exit 2" in result.message
    assert result.logs  # at least the "Running ldap-client sync" line


def test_success_returns_success_result(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _write_inventory(root)
    _save_multi_vm_config(root)

    success = MagicMock(ok=True, returncode=0)

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.run_playbook_sync",
            return_value=success,
        ),
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root"),
    ):
        result = sync_ldap_clients(root)

    assert result.ok is True
    assert result.message == "ldap-client synced on target hosts"
    assert any("Running ldap-client sync" in line for line in result.logs)


def test_writes_inventory_when_missing(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _save_multi_vm_config(root)
    # Inventory NOT pre-written -> should trigger write_inventory.

    success = MagicMock(ok=True, returncode=0)

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.run_playbook_sync",
            return_value=success,
        ),
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root"),
        patch("toolkit.core.ansible.ansible_inventory.write_inventory") as mock_write_inv,
    ):
        result = sync_ldap_clients(root)

    assert result.ok is True
    mock_write_inv.assert_called_once()


def test_on_log_callback_invoked(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _write_playbook(root)
    _write_inventory(root)
    _save_multi_vm_config(root)

    sink: list[str] = []

    def fake_run(root_arg, playbook_arg, *, inventory=None, limit=None, on_log=None):
        if on_log is not None:
            on_log("captured line")
        return MagicMock(ok=True, returncode=0)

    with (
        patch(
            "toolkit.core.identity.ldap_guest_sync.run_playbook_sync",
            side_effect=fake_run,
        ),
        patch("toolkit.core.identity.ldap_guest_sync.sync_from_repo_root"),
    ):
        result = sync_ldap_clients(root, on_log=sink.append)

    assert "captured line" in sink
    assert "captured line" in result.logs
