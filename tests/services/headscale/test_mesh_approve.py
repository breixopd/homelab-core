from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.services.headscale.bootstrap import approve_mesh_registration, personal_headscale_username


def test_personal_headscale_username_from_email():
    cfg = Config(domain="example.com", email="alice@example.com")
    assert personal_headscale_username(cfg) == "alice"


def test_approve_mesh_registration_creates_user_and_registers(monkeypatch):
    cfg = Config(domain="example.com", email="brei@example.com")
    calls: list[tuple[list[str], dict]] = []

    def fake_exec(_cfg, _container, command, _ip, _root, **kwargs):
        calls.append((command, kwargs))
        if "users" in command and "list" in command:
            return 0, "[]"
        if "users" in command and "create" in command:
            return 0, ""
        if command[:2] == ["/bin/busybox", "sh"]:
            return 0, "registered"
        return 1, "unexpected"

    monkeypatch.setattr(
        "toolkit.services.sdk.docker_exec_on_vm",
        fake_exec,
    )
    secret = "test-key-123"
    logs = approve_mesh_registration(cfg, Path("."), key=secret)
    assert any("registered node" in line for line in logs)
    register_calls = [(command, kwargs) for command, kwargs in calls if command[:2] == ["/bin/busybox", "sh"]]
    assert len(register_calls) == 1
    command, kwargs = register_calls[0]
    assert secret not in repr(command)
    assert command[-1] == "brei"
    assert kwargs["stdin"] == f"{secret}\n"
    assert "secret_environment" not in kwargs


def test_approve_mesh_registration_uses_remote_vm_and_stdin_secret(monkeypatch, tmp_path):
    cfg = Config(domain="example.com", email="brei@example.com")
    monkeypatch.setattr(type(cfg), "is_multi_node", property(lambda _self: True))
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_args: "10.0.0.2")
    calls: list[tuple[str, list[str], dict]] = []

    def fake_exec(_cfg, _container, command, ip, _root, **kwargs):
        calls.append((ip, command, kwargs))
        if "users" in command and "list" in command:
            return 0, '[{"id":77,"name":"brei"}]'
        if command[:2] == ["/bin/busybox", "sh"]:
            return 0, "registered"
        raise AssertionError(command)

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    secret = "remote-registration-key"

    logs = approve_mesh_registration(cfg, tmp_path, key=secret)

    assert logs == ["Mesh approve: registered node for user 'brei'"]
    assert all(ip == "10.0.0.2" for ip, _command, _kwargs in calls)
    register = next((command, kwargs) for _ip, command, kwargs in calls if command[:2] == ["/bin/busybox", "sh"])
    assert secret not in repr(register[0])
    assert register[1]["stdin"] == f"{secret}\n"


def test_approve_mesh_registration_fails_closed_on_invalid_users_json(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_exec(_cfg, _container, command, _ip, _root, **_kwargs):
        calls.append(command)
        return 0, "not-json"

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

    logs = approve_mesh_registration(Config(domain="example.com"), tmp_path, key="registration-key")

    assert logs == ["Mesh approve: headscale users list returned invalid JSON"]
    assert len(calls) == 1
