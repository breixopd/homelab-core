from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from toolkit.services.sdk._vmexec import docker_exec_on_vm


def test_docker_exec_on_vm_local_pipes_stdin_without_arguments(monkeypatch) -> None:
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    run = MagicMock(return_value=completed)
    cfg = MagicMock(is_multi_node=False)
    monkeypatch.setattr("toolkit.services.sdk._vmexec.subprocess.run", run)

    assert docker_exec_on_vm(cfg, "service", ["helper"], "127.0.0.1", Path("/tmp"), stdin="secret") == (0, "ok")

    assert run.call_args.args[0] == ["docker", "exec", "-i", "service", "helper"]
    assert run.call_args.kwargs["input"] == "secret"
    assert "secret" not in repr(run.call_args.args[0])


def test_docker_exec_on_vm_remote_pipes_stdin_without_command_text(monkeypatch) -> None:
    cfg = MagicMock(is_multi_node=True)
    remote = MagicMock(return_value=(0, "ok", ""))
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", remote)

    assert docker_exec_on_vm(cfg, "service", ["helper"], "10.10.10.10", Path("/tmp"), stdin="secret") == (0, "ok")

    command = remote.call_args.args[2]
    assert command == "docker exec -i service helper"
    assert remote.call_args.kwargs["stdin"] == "secret"
    assert "secret" not in command


def test_docker_exec_on_vm_remote_delivers_secret_environment_over_stdin(monkeypatch) -> None:
    cfg = MagicMock(is_multi_node=True)
    remote = MagicMock(return_value=(0, "ok", ""))
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", remote)

    assert docker_exec_on_vm(
        cfg,
        "service",
        ["helper"],
        "10.10.10.10",
        Path("/tmp"),
        secret_environment={"API_TOKEN": "test-only-token"},
    ) == (0, "ok")

    command = remote.call_args.args[2]
    assert command.startswith("docker exec -i service sh -ec ")
    assert "test-only-token" not in command
    assert remote.call_args.kwargs["stdin"].endswith("__HOMELAB_SECRET_ENV_END__\n")
    assert "API_TOKEN=test-only-token\n" in remote.call_args.kwargs["stdin"]
