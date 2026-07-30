"""ansible_ssh helper utilities."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from toolkit.core.ansible.ansible_ssh import (
    docker_exec_curl,
    resolve_ansible_ssh_key,
    sanitize_probe_output,
    scp_from_vm,
    scp_to_vm,
    should_verify_remote,
    ssh_proxy_command,
    ssh_run_on_vm,
)
from toolkit.core.config.config import Config, ProxmoxConfig
from toolkit.core.infra.proxmox_ssh import resolve_proxmox_proxy_key


def test_sanitize_probe_output_strips_traceback():
    raw = 'Traceback (most recent call last):\n  File "x.py", line 1\nurllib.error.URLError: refused'
    assert sanitize_probe_output(raw) == "urllib.error.URLError: refused"


def test_docker_exec_curl_prefers_container_exec_and_keeps_headers_off_the_command_line(monkeypatch):
    cfg = Config(domain="example.com")
    captured: list[tuple[str, str | None]] = []

    def fake_ssh(_cfg, _ip, command, **_kw):
        captured.append((command, _kw.get("stdin")))
        return 0, '{"ok": true}', ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    rc, body = docker_exec_curl(
        cfg,
        "10.10.10.12",
        "nextcloud",
        "http://localhost/status.php",
        headers={"Authorization": "Bearer test-only-secret"},
    )
    assert rc == 0
    assert body == '{"ok": true}'
    assert captured
    command, stdin = captured[0]
    assert command == "docker exec -i nextcloud curl --disable --config -"
    assert "test-only-secret" not in command
    assert stdin is not None and "test-only-secret" in stdin


def test_docker_exec_curl_verifies_https_by_default(monkeypatch) -> None:
    captured: list[str] = []

    def fake_ssh(_cfg, _ip, _command, **kwargs):
        captured.append(kwargs["stdin"])
        return 0, "ok", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    rc, body = docker_exec_curl(Config(), "10.10.10.12", "service", "https://localhost:8443/health")

    assert (rc, body) == (0, "ok")
    assert "insecure" not in captured[0].splitlines()


def test_docker_exec_curl_fallbacks_keep_headers_off_every_command_line(monkeypatch) -> None:
    captured: list[tuple[str, str | None]] = []
    responses = iter(
        [
            (127, "", "curl unavailable"),
            (0, "172.20.0.8 172.21.0.9 \n", ""),
            (127, "", "curl unavailable"),
            (0, '{"ok":true}', ""),
        ]
    )

    def fake_ssh(_cfg, _ip, command, **kwargs):
        captured.append((command, kwargs.get("stdin")))
        return next(responses)

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    rc, body = docker_exec_curl(
        Config(),
        "10.10.10.12",
        "service",
        "http://localhost:8080/health",
        headers={"Authorization": "Bearer test-only-secret"},
    )

    assert (rc, body) == (0, '{"ok":true}')
    assert [command for command, _stdin in captured[:3]] == [
        "docker exec -i service curl --disable --config -",
        "docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}} {{end}}' service",
        "curl --disable --config -",
    ]
    assert 'url = "http://172.20.0.8:8080/health"' in (captured[2][1] or "")
    assert captured[3][0].startswith("python3 -c ")
    assert all("test-only-secret" not in command for command, _stdin in captured)
    assert "test-only-secret" in (captured[0][1] or "")
    assert "test-only-secret" in (captured[2][1] or "")
    assert "test-only-secret" in (captured[3][1] or "")


def test_docker_exec_curl_sends_post_body_and_authentication_via_stdin(monkeypatch) -> None:
    captured: list[tuple[str, str | None]] = []

    def fake_ssh(_cfg, _ip, command, **kwargs):
        captured.append((command, kwargs.get("stdin")))
        return 0, "accepted", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    rc, body = docker_exec_curl(
        Config(),
        "10.10.10.12",
        "service",
        "http://localhost:8080/api/sync",
        method="POST",
        headers={"Authorization": "Bearer test-only-secret"},
        body='{"secret":"request-body"}',
    )

    assert (rc, body) == (0, "accepted")
    assert captured[0][0] == "docker exec -i service curl --disable --config -"
    assert "test-only-secret" not in captured[0][0]
    assert "request-body" not in captured[0][0]
    assert captured[0][1] is not None and 'request = "POST"' in captured[0][1]
    assert "test-only-secret" in captured[0][1]
    assert "request-body" in captured[0][1]


def test_docker_exec_curl_keeps_cookie_sessions_inside_the_target_container(monkeypatch) -> None:
    captured: list[tuple[str, str | None]] = []

    def fake_ssh(_cfg, _ip, command, **kwargs):
        captured.append((command, kwargs.get("stdin")))
        return 22, "", "session request failed"

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    rc, body = docker_exec_curl(
        Config(),
        "10.10.10.12",
        "service",
        "http://localhost:8080/login",
        cookie_file="/tmp/session.cookies",
        cookie_jar="/tmp/session.cookies",
    )

    assert rc == 22
    assert body == "session request failed"
    assert len(captured) == 1
    assert 'cookie = "/tmp/session.cookies"' in (captured[0][1] or "")
    assert 'cookie-jar = "/tmp/session.cookies"' in (captured[0][1] or "")


def test_should_verify_remote_false_on_guest(tmp_path: Path):
    cfg = Config(proxmox=ProxmoxConfig(provision_machines=True))
    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")
    with patch.dict("os.environ", {"HOMELAB_NODE": "infra"}, clear=True):
        assert should_verify_remote(cfg, tmp_path, on_guest=True) is False


def test_should_verify_remote_from_controller_placed_on_managed_node(tmp_path: Path):
    cfg = Config(proxmox=ProxmoxConfig(provision_machines=True))
    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")
    with (
        patch.dict(
            "os.environ",
            {"HOMELAB_NODE": "infra", "HOMELAB_CONTROLLER_ROLE": "local"},
            clear=True,
        ),
        patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="/usr/bin/ansible"),
    ):
        assert should_verify_remote(cfg, tmp_path, on_guest=True) is True


def test_should_verify_remote_requires_inventory(tmp_path: Path):
    cfg = Config(proxmox=ProxmoxConfig(provision_machines=True))
    with patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="/usr/bin/ansible"):
        assert should_verify_remote(cfg, tmp_path) is False


def test_resolve_ansible_ssh_key_uses_controller_local_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    key = tmp_path / "ssh" / "homelab_admin_ed25519"
    key.parent.mkdir()
    key.write_text("private\n")

    assert resolve_ansible_ssh_key(Config(), tmp_path) == key.resolve()


def test_proxmox_proxy_prefers_explicit_key_on_workstation(tmp_path: Path, monkeypatch):
    explicit = tmp_path / "pve-key"
    explicit.write_text("private\n")
    automation = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation.parent.mkdir()
    automation.write_text("controller\n")
    cfg = Config(proxmox={"ssh": {"key_file": str(explicit)}})
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)

    assert resolve_proxmox_proxy_key(cfg, tmp_path) == explicit.resolve()


def test_proxmox_proxy_uses_controller_key_only_on_control_node(tmp_path: Path, monkeypatch):
    automation = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation.parent.mkdir()
    automation.write_text("controller\n")
    cfg = Config(proxmox={"ssh": {"key_file": str(tmp_path / "missing")}})

    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    assert resolve_proxmox_proxy_key(cfg, tmp_path) == automation.resolve()

    monkeypatch.setenv("HOMELAB_NODE", "apps")
    assert resolve_proxmox_proxy_key(cfg, tmp_path) is None


def test_proxmox_proxy_fails_closed_without_any_key(tmp_path: Path, monkeypatch):
    cfg = Config(proxmox={"ssh": {"key_file": str(tmp_path / "missing")}})
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    assert resolve_proxmox_proxy_key(cfg, tmp_path) is None


def test_ssh_proxy_command_renders_controller_identity_on_control_node(tmp_path: Path, monkeypatch):
    automation = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation.parent.mkdir()
    automation.write_text("controller\n")
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)

    command = ssh_proxy_command(cfg, tmp_path)

    assert f"-i {automation}" in command
    assert "root@203.0.113.10" in command


def test_ssh_proxy_command_fails_closed_on_workstation_without_explicit_key(tmp_path: Path, monkeypatch):
    automation = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation.parent.mkdir()
    automation.write_text("controller\n")
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    monkeypatch.delenv("HOMELAB_NODE", raising=False)

    assert ssh_proxy_command(cfg, tmp_path) == ""


def test_ssh_run_on_vm_uses_controller_proxy_when_guest_is_not_directly_reachable(tmp_path: Path, monkeypatch):
    automation = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation.parent.mkdir()
    automation.write_text("controller\n")
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh._is_directly_reachable", lambda *_args: False)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh._local_network_ips", lambda: [])
    captured: list[list[str]] = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.subprocess.run", fake_run)

    rc, stdout, _stderr = ssh_run_on_vm(cfg, "10.10.10.12", "true", root=tmp_path)

    assert (rc, stdout) == (0, "ok")
    assert captured
    rendered = " ".join(captured[0])
    assert "ProxyCommand=ssh -i" in rendered
    assert str(automation) in rendered


@pytest.mark.parametrize(
    ("copy", "message"),
    ((scp_to_vm, "scp to 10.10.10.12 timed out after 7s"), (scp_from_vm, "scp from 10.10.10.12 timed out after 7s")),
)
def test_scp_helpers_report_timeouts_as_runtime_errors(tmp_path: Path, monkeypatch, copy, message: str) -> None:
    source = tmp_path / "source"
    source.write_text("payload")
    cfg = Config(domain="lab.test", dns={"public_ip": "203.0.113.10"})

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_argv", lambda *_args, **_kwargs: ["scp"])
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("scp", 7)),
    )

    with pytest.raises(RuntimeError, match=message):
        if copy is scp_to_vm:
            copy(cfg, tmp_path, source, "10.10.10.12", "/tmp/target", timeout=7)
        else:
            copy(cfg, tmp_path, "10.10.10.12", "/tmp/source", source, timeout=7)
