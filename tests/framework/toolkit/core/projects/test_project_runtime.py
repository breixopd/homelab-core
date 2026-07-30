from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config, ProjectEntry
from toolkit.core.projects.runtime import run_project_command

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _config() -> Config:
    cfg = Config(domain="example.test")
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="demo",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=PINNED_IMAGE,
            placement="apps",
            container_port=45678,
        )
    ]
    return cfg


def test_remote_project_action_targets_declared_container(monkeypatch, tmp_path: Path) -> None:
    observed: list[tuple[str, str]] = []

    def fake_ssh(cfg, ip, command, *, root, timeout, retries):
        observed.append((ip, command))
        return 0, "demo\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    result = run_project_command(tmp_path, _config(), "demo", "restart")

    assert result.ok is True
    assert observed == [("10.10.10.12", "docker restart demo")]


def test_project_command_rejects_unknown_identity(tmp_path: Path) -> None:
    result = run_project_command(tmp_path, _config(), "missing", "status")

    assert result.ok is False
    assert result.output == "Project 'missing' is not registered"


def test_project_logs_are_bounded(monkeypatch, tmp_path: Path) -> None:
    observed: list[str] = []

    def fake_ssh(cfg, ip, command, *, root, timeout, retries):
        observed.append(command)
        return 0, "line one\nline two\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    result = run_project_command(tmp_path, _config(), "demo", "logs")

    assert result.ok is True
    assert observed == ["docker logs --tail 200 demo"]
