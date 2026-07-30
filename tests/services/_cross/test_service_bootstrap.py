"""Tests for service bootstrap helpers (now living in per-service bootstrap.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers.machines import renamed_default_machines
from toolkit.core.config.config import Config, ExternalHost, save_config
from toolkit.services.headscale.bootstrap import ensure_controller_mesh_joined
from toolkit.services.kopia.bootstrap import _ensure_agent_users, bootstrap_kopia_repository
from toolkit.services.lldap.bootstrap import bootstrap_lldap_user, sync_ldap_bind_only
from toolkit.services.seerr.bootstrap import extract_seerr_api_key


def test_extract_seerr_api_key_from_settings(tmp_path: Path):
    settings_dir = tmp_path / "data" / "seerr" / "config"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(json.dumps({"main": {"apiKey": "abc123secret"}}))
    assert extract_seerr_api_key(tmp_path) == "abc123secret"


def test_extract_seerr_api_key_missing(tmp_path: Path):
    assert extract_seerr_api_key(tmp_path) is None


def test_bootstrap_kopia_connects_existing_repository(monkeypatch):
    calls = []

    def fake_docker_exec(service, cmd, **kwargs):
        calls.append((service, cmd, kwargs))
        if cmd[:3] == ["kopia", "repository", "status"]:
            return 1, "repository is not connected"
        if cmd[:4] == ["kopia", "repository", "connect", "filesystem"]:
            return 0, "Connected to repository."
        if cmd[:3] == ["kopia", "policy", "set"]:
            return 0, "policy set"
        if cmd[:2] == ["sh", "-ec"] and " users set " in cmd[-1]:
            return 1, "user not found"
        if cmd[:2] == ["sh", "-ec"] and " users add " in cmd[-1]:
            return 0, "user added"
        if cmd[:2] == ["/bin/sh", "-c"]:
            return 0, ""
        raise AssertionError(f"unexpected Kopia command: {cmd}")

    monkeypatch.setattr("toolkit.services.kopia.bootstrap.docker_exec", fake_docker_exec)

    logs = bootstrap_kopia_repository(
        Config(), {"KOPIA_REPOSITORY_PASSWORD": "repo-password", "KOPIA_AGENT_PASSWORD": "agent-password"}
    )

    assert any("filesystem repository connected" in line for line in logs)
    assert all(kwargs["environment"]["KOPIA_CONFIG_PATH"] == "/app/config/repository.config" for _, _, kwargs in calls)
    connect = next(call for call in calls if call[1][:4] == ["kopia", "repository", "connect", "filesystem"])
    assert connect[1] == [
        "kopia",
        "repository",
        "connect",
        "filesystem",
        "--path=/repository",
        "--override-hostname=homelab-infra",
        "--override-username=kopia",
    ]
    assert connect[2]["secret_environment"] == {"KOPIA_PASSWORD": "repo-password"}
    for role in ("media", "apps"):
        user_call = next(call for call in calls if f" users add homelab@homelab-{role} " in call[1][-1])
        assert user_call[1][:2] == ["sh", "-ec"]
        assert "--ask-password" in user_call[1][-1]
        assert "agent-password" not in repr(user_call[1])
        assert user_call[2]["secret_environment"] == {
            "KOPIA_PASSWORD": "repo-password",
            "KOPIA_AGENT_PASSWORD": "agent-password",
        }


def test_bootstrap_kopia_connects_remote_sftp_repository(monkeypatch):
    calls = []

    def fake_docker_exec(service, cmd, **kwargs):
        calls.append((service, cmd, kwargs))
        if cmd[:3] == ["kopia", "repository", "status"]:
            return 1, "repository is not connected"
        if cmd[:4] == ["kopia", "repository", "connect", "sftp"]:
            return 0, "Connected to repository."
        if cmd[:3] == ["kopia", "policy", "set"] or (cmd[:2] == ["sh", "-ec"] and " users set " in cmd[-1]):
            return 0, "ok"
        if cmd[:2] == ["/bin/sh", "-c"]:
            return 0, ""
        raise AssertionError(f"unexpected Kopia command: {cmd}")

    monkeypatch.setattr("toolkit.services.kopia.bootstrap.docker_exec", fake_docker_exec)
    host = ExternalHost(
        name="nas",
        ip="10.10.10.20",
        ssh_user="backup",
        ssh_port=2222,
        services=["backup-storage"],
        integrations={"backup-storage": {"path": "/srv/kopia"}},
    )
    cfg = Config(
        backups={"enabled": True, "target": "remote", "storage_host": "nas"},
        external_hosts=[host],
    )

    logs = bootstrap_kopia_repository(
        cfg,
        {"KOPIA_REPOSITORY_PASSWORD": "repo-password", "KOPIA_AGENT_PASSWORD": "agent-password"},
    )

    assert any("SFTP repository connected" in line for line in logs)
    command = next(cmd for _service, cmd, _env in calls if cmd[:4] == ["kopia", "repository", "connect", "sftp"])
    assert "--host=10.10.10.20" in command
    assert "--port=2222" in command
    assert "--username=backup" in command
    assert "--path=/srv/kopia" in command
    assert "--keyfile=/app/config/remote_ed25519" in command
    assert "--known-hosts=/app/config/known_hosts" in command


def test_bootstrap_kopia_disconnects_mismatched_backend_before_remote_connect(monkeypatch):
    calls: list[list[str]] = []

    def fake_docker_exec(_service, command, **_kwargs):
        calls.append(command)
        if command[:3] == ["kopia", "repository", "status"]:
            return 0, "Connected to filesystem repository"
        if command[:3] == ["kopia", "repository", "disconnect"]:
            return 0, "disconnected"
        if command[:4] == ["kopia", "repository", "connect", "sftp"]:
            return 0, "connected"
        if command[:3] == ["kopia", "policy", "set"] or (command[:2] == ["sh", "-ec"] and " users set " in command[-1]):
            return 0, "ok"
        if command[:2] == ["/bin/sh", "-c"]:
            return 0, ""
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.kopia.bootstrap.docker_exec", fake_docker_exec)
    host = ExternalHost(
        name="nas",
        ip="10.10.10.20",
        services=["backup-storage"],
        integrations={"backup-storage": {"path": "/srv/kopia"}},
    )
    cfg = Config(
        backups={"enabled": True, "target": "remote", "storage_host": "nas"},
        external_hosts=[host],
    )

    logs = bootstrap_kopia_repository(
        cfg,
        {"KOPIA_REPOSITORY_PASSWORD": "repo-password", "KOPIA_AGENT_PASSWORD": "agent-password"},
    )

    assert ["kopia", "repository", "disconnect"] in calls
    assert any("switching to SFTP" in line for line in logs)


def test_kopia_agent_user_provisioning_keeps_credentials_off_command_line(monkeypatch) -> None:
    calls = []

    def fake_docker_exec(service, command, **kwargs):
        calls.append((service, command, kwargs))
        action = command[-1] if command else ""
        if " users set " in action:
            return 1, "user not found"
        if " users add " in action:
            return 0, "user added"
        if command[:2] == ["/bin/sh", "-c"]:
            return 0, ""
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.kopia.bootstrap.docker_exec", fake_docker_exec)

    logs: list[str] = []
    _ensure_agent_users(
        Config(),
        {"KOPIA_CONFIG_PATH": "/app/config/repository.config"},
        {"KOPIA_PASSWORD": "repo-test-password"},
        "agent-test-password",
        logs,
    )

    agent_calls = [call for call in calls if call[1][:2] == ["sh", "-ec"]]
    assert agent_calls
    for _service, command, kwargs in agent_calls:
        assert "agent-test-password" not in repr(command)
        assert "--ask-password" in command[-1]
        assert "--user-password=" not in command[-1]
        assert kwargs["secret_environment"] == {
            "KOPIA_PASSWORD": "repo-test-password",
            "KOPIA_AGENT_PASSWORD": "agent-test-password",
        }


def test_bootstrap_lldap_user_delegates_to_client(monkeypatch):
    client = MagicMock()
    client.ensure_posix_schema.return_value = ["schema: user attribute gidNumber"]
    client.ensure_service_bind.return_value = ["created service account ldap-bind"]
    client.ensure_homelab_users_group.return_value = []
    client.ensure_homelab_group_gids.return_value = []
    client.ensure_owner.return_value = ["LLDAP: user brei already exists", "LLDAP: password updated for brei"]
    client.ensure_all_users_posix.return_value = []
    client.ensure_homelab_groups.return_value = ["created group homelab-media"]
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.LLDAPClient",
        lambda **kwargs: client,
    )

    logs = bootstrap_lldap_user(
        Config(domain="example.com", email="brei@example.com", owner_username="brei"),
        {
            "LLDAP_ADMIN_PASSWORD": "admin-password",
            "LLDAP_BIND_PASSWORD": "bind-password",
            "SSO_USER_PASSWORD": "configured-owner-password",
        },
    )

    # POSIX schema must be created before the service bind account, because
    # ensure_service_bind() -> ensure_user_posix() writes gidNumber/uidNumber.
    call_order = [m[0] for m in client.method_calls]
    assert call_order.index("ensure_posix_schema") < call_order.index("ensure_service_bind")
    client.ensure_service_bind.assert_called_once_with("bind-password", domain="example.com")
    client.ensure_owner.assert_called_once_with(
        "brei@example.com",
        "configured-owner-password",
        domain="example.com",
        groups=["lldap_admin", "homelab-media", "homelab-cloud", "homelab-admin"],
        user_id="brei",
    )
    client.ensure_homelab_groups.assert_called_once()
    assert any("password updated" in line for line in logs)
    assert any("schema" in line for line in logs)


def test_bootstrap_lldap_user_reports_client_errors(monkeypatch):
    client = MagicMock()
    client.ensure_owner.side_effect = RuntimeError("LLDAP admin login failed (HTTP 401)")
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.LLDAPClient",
        lambda **kwargs: client,
    )

    logs = bootstrap_lldap_user(
        Config(domain="example.com", email="brei@example.com"),
        {"LLDAP_ADMIN_PASSWORD": "wrong"},
    )

    assert any("user bootstrap failed" in line for line in logs)


def test_lldap_recovery_loads_secrets_from_custom_control_machine(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(domain="example.com", machines=renamed_default_machines())
    save_config(cfg, tmp_path)
    client = MagicMock()
    client.ensure_service_bind.return_value = []
    load_secrets = MagicMock(
        return_value={
            "LLDAP_ADMIN_PASSWORD": "admin-password",
            "LLDAP_BIND_PASSWORD": "bind-password",
        }
    )
    monkeypatch.setattr("toolkit.core.secrets.secrets.load_runtime_secrets", load_secrets)
    monkeypatch.setattr("toolkit.core.identity.lldap_client.LLDAPClient", lambda **_kwargs: client)

    sync_ldap_bind_only(tmp_path)

    load_secrets.assert_called_once_with(tmp_path, role="core")


def test_ensure_controller_mesh_joined_skipped_without_opt_in(monkeypatch):
    monkeypatch.delenv("HOMELAB_JOIN_CONTROLLER_MESH", raising=False)
    cfg = Config(domain="example.com", email="admin@example.com")
    logs = ensure_controller_mesh_joined(cfg)
    assert any("skipped" in line.lower() for line in logs)
