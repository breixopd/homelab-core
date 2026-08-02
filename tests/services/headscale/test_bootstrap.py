"""Security regressions for Headscale bootstrap output."""

from __future__ import annotations

import subprocess

from toolkit.core.config.config import Config
from toolkit.services.headscale.bootstrap import (
    _parse_headscale_preauth_output,
    bootstrap_headscale_preauth,
    ensure_controller_mesh_joined,
    headscale_preauth_key,
    headscale_preauth_key_for_deploy,
)


def test_preauth_output_parser_fails_closed_on_malformed_list_rows() -> None:
    assert _parse_headscale_preauth_output('["not-an-object"]') is None


def test_preauth_output_parser_rejects_non_json_output() -> None:
    assert _parse_headscale_preauth_output("warning: not a credential") is None


def test_preauth_output_parser_rejects_non_string_key() -> None:
    assert _parse_headscale_preauth_output('{"key":true}') is None


def test_bootstrap_preauth_checks_issuance_without_creating_a_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap._local_homelab_user_id",
        lambda: "42",
    )

    logs = bootstrap_headscale_preauth(tags=["tag:fleet-external"])

    assert logs == ["Headscale: preauth prerequisites ready"]


def test_bootstrap_preauth_failure_log_contains_no_credential_material(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap._local_homelab_user_id",
        lambda: None,
    )

    logs = bootstrap_headscale_preauth()

    assert logs == ["Headscale: preauth prerequisites unavailable"]


def test_preauth_uses_exact_homelab_user_id(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_exec(_container, command):
        calls.append(command)
        if command[-2:] == ["users", "list"]:
            return 0, '[{"id": 9, "name": "other"}, {"id": 42, "name": "homelab"}]'
        if "preauthkeys" in command and "create" in command:
            assert command[command.index("--user") + 1] == "42"
            assert "--reusable" not in command
            assert command[command.index("--expiration") + 1] == "1h"
            return 0, '{"key":"hskey-full"}'
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.docker_exec", fake_exec)
    assert headscale_preauth_key() == "hskey-full"
    assert any("--user" in command and command[command.index("--user") + 1] == "42" for command in calls)


def test_preauth_creates_missing_homelab_user_then_resolves_id(monkeypatch) -> None:
    responses = iter(
        [
            (0, '[{"id": 7, "name": "other"}]'),
            (0, ""),
            (0, '[{"id": 99, "name": "homelab"}]'),
            (0, '{"key":"hskey-created"}'),
        ]
    )
    calls: list[list[str]] = []

    def fake_exec(_container, command):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.docker_exec", fake_exec)
    assert headscale_preauth_key() == "hskey-created"
    create = calls[-1]
    assert create[create.index("--user") + 1] == "99"
    assert ["headscale", "users", "create", "homelab"] in calls


def test_preauth_fails_closed_on_malformed_or_ambiguous_users(monkeypatch) -> None:
    for payload in ("not-json", '[{"id": 1, "name": "homelab"}, {"id": 2, "name": "homelab"}]'):
        monkeypatch.setattr(
            "toolkit.services.headscale.bootstrap.docker_exec",
            lambda *_args, payload=payload: (0, payload),
        )
        assert headscale_preauth_key() is None


def test_preauth_fails_closed_when_one_time_key_creation_fails(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_exec(_container, command):
        calls.append(command)
        if command[-2:] == ["users", "list"]:
            return 0, '[{"id": 42, "name": "homelab"}]'
        if "preauthkeys" in command and "create" in command:
            return 1, "control server unavailable"
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.docker_exec", fake_exec)

    assert headscale_preauth_key() is None
    assert sum("preauthkeys" in command and "create" in command for command in calls) == 1


def test_multi_node_preauth_uses_resolved_homelab_id(monkeypatch, tmp_path) -> None:
    cfg = Config(domain="example.com")
    monkeypatch.setattr(type(cfg), "is_multi_node", property(lambda _self: True))
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_args: "10.0.0.2")
    calls: list[str] = []

    def fake_ssh(_cfg, _ip, command, **_kwargs):
        calls.append(command)
        if command.endswith("users list"):
            return 0, '[{"id": 4, "name": "other"}, {"id": 77, "name": "homelab"}]', ""
        if "preauthkeys create" in command:
            assert "-u 77 " in command
            assert "--reusable" not in command
            assert "-e 1h" in command
            return 0, '{"key":"hskey-multi-node"}', ""
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    assert headscale_preauth_key_for_deploy(cfg, tmp_path) == "hskey-multi-node"
    assert any("users list" in command for command in calls)
    assert all("-u 1" not in command for command in calls)


def test_multi_node_bootstrap_checks_remote_prerequisites(monkeypatch, tmp_path) -> None:
    cfg = Config(domain="example.com")
    monkeypatch.setattr(type(cfg), "is_multi_node", property(lambda _self: True))
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap._remote_homelab_user_id",
        lambda actual_cfg, actual_root: "77" if actual_cfg is cfg and actual_root == tmp_path else None,
    )
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap._local_homelab_user_id",
        lambda: (_ for _ in ()).throw(AssertionError("multi-node readiness must not use local Docker")),
    )

    assert bootstrap_headscale_preauth(cfg, tmp_path) == ["Headscale: preauth prerequisites ready"]


def test_mesh_recovery_hint_uses_real_command_without_key_material(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_JOIN_CONTROLLER_MESH", "1")
    up_commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        if command[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(command, 1, "", "not running")
        if command[:2] == ["tailscale", "up"]:
            up_commands.append(command)
            return subprocess.CompletedProcess(command, 1, "", "Access denied")
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.subprocess.run", fake_run)
    secret = "hskey-auth-never-log-this"
    logs = ensure_controller_mesh_joined(
        Config(domain="example.com"),
        preauth_key=secret,
        fleet=True,
        hostname="custom-controller",
    )
    text = "\n".join(logs)
    assert "mesh join-cmd" not in text
    assert "sudo -E homelab-toolkit mesh join --fleet" in text
    assert secret not in text
    assert "--hostname=custom-controller" in up_commands[0]


def test_mesh_join_success_requires_running_headscale_control_state(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_JOIN_CONTROLLER_MESH", "1")
    status_calls = 0

    def fake_run(command, **_kwargs):
        nonlocal status_calls
        if command[:2] == ["tailscale", "status"]:
            status_calls += 1
            if status_calls == 1:
                return subprocess.CompletedProcess(command, 1, "", "not running")
            return subprocess.CompletedProcess(command, 0, '{"BackendState":"Running"}', "")
        if command[:2] == ["tailscale", "up"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["tailscale", "debug", "prefs"]:
            return subprocess.CompletedProcess(command, 0, '{"ControlURL":"https://vpn.example.com"}', "")
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.subprocess.run", fake_run)

    logs = ensure_controller_mesh_joined(
        Config(domain="example.com"),
        preauth_key="hskey-one-time",
        fleet=True,
    )

    assert logs == ["Headscale: fleet node joined mesh"]


def test_mesh_join_timeout_does_not_expose_one_time_key(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_JOIN_CONTROLLER_MESH", "1")
    secret = "hskey-never-include-in-timeout"

    def fake_run(command, **_kwargs):
        if command[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(command, 1, "", "not running")
        if command[:2] == ["tailscale", "up"]:
            raise subprocess.TimeoutExpired(command, 120)
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.headscale.bootstrap.subprocess.run", fake_run)

    logs = ensure_controller_mesh_joined(Config(domain="example.com"), preauth_key=secret, fleet=True)

    assert logs == ["Headscale: tailscale up timed out; mesh state was not verified"]
    assert secret not in "\n".join(logs)
