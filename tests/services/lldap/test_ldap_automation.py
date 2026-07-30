"""Unit tests for toolkit.core.identity.ldap_automation."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from tests.helpers.machines import single_control_machines
from toolkit.core.config.config import Config, ServicesConfig, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.identity.ldap_automation import (
    _controller_remote,
    _repair_posix_with_client,
    ensure_directory_and_sssd,
    repair_directory_posix,
    sync_sssd_after_hooks,
    sync_sssd_guests,
)
from toolkit.core.identity.ldap_guest_sync import LdapSyncResult


def _multi_vm_cfg() -> Config:
    return Config(domain="example.com", email="admin@example.com")


def _single_vm_cfg() -> Config:
    return Config(
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
        machines=single_control_machines(),
    )


def _save_config(root: Path, cfg: Config) -> None:
    save_config(cfg, config_path(root))


def _write_secrets(root: Path, *, admin_password: str = "secret") -> Path:
    secrets = root / "secrets.enc.yaml"
    secrets.write_text(f"LLDAP_ADMIN_PASSWORD: {admin_password}\n")
    return secrets


def test_controller_remote_true_when_env_and_multi_vm(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    with patch.dict(os.environ, {"HOMELAB_DEPLOY_CONTROLLER": "1"}):
        assert _controller_remote(root) is True


def test_controller_remote_false_when_env_unset(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    env = {k: v for k, v in os.environ.items() if k != "HOMELAB_DEPLOY_CONTROLLER"}
    with patch.dict(os.environ, env, clear=True):
        assert _controller_remote(root) is False


def test_controller_remote_false_when_single_vm(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _single_vm_cfg())

    with patch.dict(os.environ, {"HOMELAB_DEPLOY_CONTROLLER": "1"}):
        assert _controller_remote(root) is False


def test_repair_directory_posix_remote_branch_uses_ssh(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    _write_secrets(root, admin_password="admin-pw")

    remote_lines = ["LDAP: schema ok", "LDAP: posix ok"]

    with (
        patch.dict(os.environ, {"HOMELAB_DEPLOY_CONTROLLER": "1"}),
        patch(
            "toolkit.core.identity.ldap_automation._run_on_infra",
            return_value=remote_lines,
        ) as mock_run,
        patch("toolkit.core.identity.ldap_automation._repair_posix_local") as mock_local,
    ):
        result = repair_directory_posix(root)

    assert result == remote_lines
    mock_local.assert_not_called()
    mock_run.assert_called_once()
    # Ensure the admin password was forwarded via env kwarg.
    _args, kwargs = mock_run.call_args
    assert kwargs.get("extra_env", {}).get("LLDAP_ADMIN_PASSWORD") == "admin-pw"


def test_repair_directory_posix_remote_missing_password(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    # No secrets file => no admin password available.

    with (
        patch.dict(os.environ, {"HOMELAB_DEPLOY_CONTROLLER": "1"}),
        patch("toolkit.core.identity.ldap_automation._run_on_infra") as mock_run,
        patch("toolkit.core.identity.ldap_automation._repair_posix_local") as mock_local,
    ):
        result = repair_directory_posix(root)

    assert any("LLDAP_ADMIN_PASSWORD missing" in ln for ln in result)
    mock_run.assert_not_called()
    mock_local.assert_not_called()


def test_repair_directory_posix_local_branch(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    _write_secrets(root, admin_password="local-pw")

    env = {k: v for k, v in os.environ.items() if k != "HOMELAB_DEPLOY_CONTROLLER"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch(
            "toolkit.core.identity.ldap_automation._repair_posix_local",
            return_value=["LDAP: local repair ok"],
        ) as mock_local,
        patch("toolkit.core.identity.ldap_automation._run_on_infra") as mock_run,
    ):
        result = repair_directory_posix(root)

    assert result == ["LDAP: local repair ok"]
    mock_local.assert_called_once_with(root)
    mock_run.assert_not_called()


def test_repair_posix_with_client_aggregates_logs():
    client = MagicMock()
    client.ensure_posix_schema.return_value = ["schema"]
    client.ensure_homelab_users_group.return_value = ["group"]
    client.ensure_homelab_group_gids.return_value = ["gids"]
    client.ensure_all_users_posix.return_value = ["users"]

    logs = _repair_posix_with_client(client)

    assert logs == ["schema", "group", "gids", "users"]


def test_repair_posix_with_client_swallows_runtime_error():
    client = MagicMock()
    client.ensure_posix_schema.side_effect = RuntimeError("boom")
    client.ensure_homelab_users_group.return_value = []
    client.ensure_homelab_group_gids.return_value = []
    client.ensure_all_users_posix.return_value = []

    logs = _repair_posix_with_client(client)

    assert any("POSIX repair failed" in ln and "boom" in ln for ln in logs)


def test_sync_sssd_guests_single_host_short_circuits(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _single_vm_cfg())

    # The module reads cfg.fleet.nodes (a list). Use a config-like mock so the
    # single-host short-circuit fires without depending on FleetConfig shape.
    mock_cfg = MagicMock()
    mock_cfg.is_multi_node = False
    mock_cfg.fleet.nodes = []

    with (
        patch(
            "toolkit.core.identity.ldap_automation.load_config",
            return_value=mock_cfg,
        ),
        patch("toolkit.core.identity.ldap_automation.sync_ldap_clients") as mock_sync,
    ):
        result = sync_sssd_guests(root)

    assert result == ["LDAP: single-host — SSSD sync not needed"]
    mock_sync.assert_not_called()


def test_sync_sssd_guests_delegates_to_sync_ldap_clients(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    sync_result = LdapSyncResult(ok=True, message="ldap-client synced", logs=["line-a", "line-b"])
    with patch(
        "toolkit.core.identity.ldap_automation.sync_ldap_clients",
        return_value=sync_result,
    ) as mock_sync:
        result = sync_sssd_guests(root, limit="apps")

    assert result == ["line-a", "line-b"]
    mock_sync.assert_called_once_with(root, limit="apps")


def test_sync_sssd_guests_returns_message_when_logs_empty(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    sync_result = LdapSyncResult(ok=True, message="nothing to do", logs=[])
    with patch(
        "toolkit.core.identity.ldap_automation.sync_ldap_clients",
        return_value=sync_result,
    ):
        result = sync_sssd_guests(root)

    assert result == ["nothing to do"]


def test_ensure_directory_and_sssd_calls_sync_ldap_clients_with_limit(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    with (
        patch(
            "toolkit.core.identity.ldap_automation.repair_directory_posix",
            return_value=["LDAP: repair ok"],
        ) as mock_repair,
        patch(
            "toolkit.core.identity.ldap_automation.sync_ldap_clients",
            return_value=LdapSyncResult(ok=True, message="synced", logs=["LDAP: sssd ok"]),
        ) as mock_sync,
    ):
        result = ensure_directory_and_sssd(root, limit="apps", repair=True)

    assert result == ["LDAP: repair ok", "LDAP: sssd ok"]
    mock_repair.assert_called_once_with(root)
    mock_sync.assert_called_once_with(root, limit="apps")


def test_ensure_directory_and_sssd_skips_repair_when_disabled(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    with (
        patch("toolkit.core.identity.ldap_automation.repair_directory_posix") as mock_repair,
        patch(
            "toolkit.core.identity.ldap_automation.sync_ldap_clients",
            return_value=LdapSyncResult(ok=True, message="synced", logs=["LDAP: sssd ok"]),
        ),
    ):
        result = ensure_directory_and_sssd(root, limit="infra", repair=False)

    assert result == ["LDAP: sssd ok"]
    mock_repair.assert_not_called()


def test_sync_sssd_after_hooks_skips_single_vm(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _single_vm_cfg()
    _save_config(root, cfg)

    with patch("toolkit.core.identity.ldap_automation.ensure_directory_and_sssd") as mock_ensure:
        result = sync_sssd_after_hooks(root, cfg)

    assert result == []
    mock_ensure.assert_not_called()


def test_sync_sssd_after_hooks_runs_for_multi_vm(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _multi_vm_cfg()
    _save_config(root, cfg)

    with patch(
        "toolkit.core.identity.ldap_automation.ensure_directory_and_sssd",
        return_value=["LDAP: sssd refreshed"],
    ) as mock_ensure:
        result = sync_sssd_after_hooks(root, cfg)

    assert result == ["LDAP: sssd refreshed"]
    mock_ensure.assert_called_once_with(root, repair=False)


def test_run_on_infra_rejects_config_without_enabled_control_node(tmp_path: Path):
    from toolkit.core.identity.ldap_automation import _run_on_infra

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _multi_vm_cfg()
    cfg.machines["infra"] = cfg.machines["infra"].model_copy(update={"enabled": False})
    _save_config(root, cfg)

    with patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm") as mock_ssh:
        with pytest.raises(ValidationError, match="exactly one enabled machine must have the control label"):
            _run_on_infra(root, "print('hi')")

    mock_ssh.assert_not_called()


def test_run_on_infra_invokes_ssh_with_expected_command(tmp_path: Path):
    from toolkit.core.identity.ldap_automation import _run_on_infra

    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "schema-ok\nposix-ok\n", ""),
    ) as mock_ssh:
        result = _run_on_infra(
            root,
            "print('hi')",
            extra_env={"LLDAP_ADMIN_PASSWORD": "s3cret"},
        )

    assert result == ["schema-ok", "posix-ok"]
    mock_ssh.assert_called_once()
    call_args, call_kwargs = mock_ssh.call_args
    cfg_arg, ip_arg, cmd_arg = call_args[:3]
    assert ip_arg == "10.10.10.10"
    assert "PYTHONPATH=/opt/homelab" in cmd_arg
    assert "HOMELAB_NODE=infra" in cmd_arg
    assert "LLDAP_ADMIN_PASSWORD=s3cret" in cmd_arg
    assert call_kwargs.get("root") == root
    assert call_kwargs.get("timeout") == 180


def test_run_on_infra_appends_failure_on_nonzero_exit(tmp_path: Path):
    from toolkit.core.identity.ldap_automation import _run_on_infra

    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())

    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(1, "", "boom"),
    ) as mock_ssh:
        result = _run_on_infra(root, "print('hi')")

    assert any("LDAP remote failed (exit 1)" in ln and "boom" in ln for ln in result)
    mock_ssh.assert_called_once()


def test_repair_posix_local_uses_env_password_first(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    _write_secrets(root, admin_password="from-secrets")

    with (
        patch.dict(os.environ, {"LLDAP_ADMIN_PASSWORD": "from-env"}),
        patch("toolkit.core.identity.ldap_automation.LLDAPClient") as mock_client_cls,
        patch(
            "toolkit.core.identity.ldap_automation._repair_posix_with_client",
            return_value=["LDAP: repaired"],
        ) as mock_repair,
    ):
        from toolkit.core.identity.ldap_automation import _repair_posix_local

        result = _repair_posix_local(root)

    assert result == ["LDAP: repaired"]
    mock_client_cls.assert_called_once_with(admin_password="from-env", root=root)
    mock_repair.assert_called_once()


def test_repair_posix_local_falls_back_to_secrets(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    _write_secrets(root, admin_password="from-secrets")

    env = {k: v for k, v in os.environ.items() if k != "LLDAP_ADMIN_PASSWORD"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("toolkit.core.identity.ldap_automation.LLDAPClient") as mock_client_cls,
        patch(
            "toolkit.core.identity.ldap_automation._repair_posix_with_client",
            return_value=["LDAP: repaired"],
        ),
    ):
        from toolkit.core.identity.ldap_automation import _repair_posix_local

        result = _repair_posix_local(root)

    assert result == ["LDAP: repaired"]
    mock_client_cls.assert_called_once_with(admin_password="from-secrets", root=root)


def test_repair_posix_local_skips_when_no_password(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _save_config(root, _multi_vm_cfg())
    # No secrets file, no env password.

    env = {k: v for k, v in os.environ.items() if k != "LLDAP_ADMIN_PASSWORD"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("toolkit.core.identity.ldap_automation.LLDAPClient") as mock_client_cls,
    ):
        from toolkit.core.identity.ldap_automation import _repair_posix_local

        result = _repair_posix_local(root)

    assert any("LLDAP_ADMIN_PASSWORD missing" in ln for ln in result)
    mock_client_cls.assert_not_called()
