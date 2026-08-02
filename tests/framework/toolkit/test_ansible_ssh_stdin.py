from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import toolkit.core.ansible.ansible_ssh as ssh_transport
from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import Config
from toolkit.core.machines import MachineSpec


def test_local_vm_command_receives_secret_only_over_stdin(monkeypatch) -> None:
    cfg = MagicMock()
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh._is_local_ip", lambda _ip, _local_ips=None: True)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.run_text_process_group", run)

    result = ssh_run_on_vm(
        cfg,
        "10.0.0.10",
        "fixed-helper-command",
        root=Path("/tmp/test-root"),
        stdin="secret-payload",
    )

    assert result == (0, "ok", "")
    args, kwargs = run.call_args
    assert args[0] == ["bash", "-c", "fixed-helper-command"]
    assert kwargs["input_text"] == "secret-payload"
    assert "secret-payload" not in repr(args)


def test_local_vm_network_preflight_consumes_the_same_deadline(monkeypatch) -> None:
    cfg = MagicMock()
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    run = MagicMock(return_value=completed)
    clock = iter([100.0, 100.0, 101.2])

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh._local_network_ips", lambda **_kwargs: ["10.0.0.10"])
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.run_text_process_group", run)

    assert ssh_run_on_vm(cfg, "10.0.0.10", "true", timeout=10, deadline=105.0) == (0, "ok", "")
    assert run.call_args.kwargs["timeout"] == pytest.approx(3.8)


def test_remote_vm_reuses_owner_only_ssh_control_socket(tmp_path, monkeypatch) -> None:
    cfg = Config(
        machines={
            "control": MachineSpec(
                hostname="control",
                address="10.0.0.10",
                gateway="10.0.0.1",
                vmid=800,
                labels=("control",),
                ssh_user="operator",
                ssh_port=2222,
            )
        }
    )
    completed = MagicMock(returncode=0, stdout="ok", stderr="")
    run = MagicMock(return_value=completed)
    key = tmp_path / "deploy-key"
    key.write_text("test")

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh._is_local_ip", lambda _ip, _local_ips=None: False)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh._is_directly_reachable",
        lambda _ip, _cidr, _local_ips=None: False,
    )
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.resolve_ansible_ssh_key", lambda *_a: key)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_proxy_command", lambda *_a: "ssh-jump")
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.run_text_process_group", run)

    assert ssh_run_on_vm(cfg, "10.0.0.10", "true", root=tmp_path) == (0, "ok", "")

    command = run.call_args.args[0]
    options = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-o"]
    control_path = next(value.split("=", 1)[1] for value in options if value.startswith("ControlPath="))
    assert "ControlMaster=auto" in options
    assert "ControlPersist=120" in options
    assert "IdentitiesOnly=yes" in options
    assert "IdentityAgent=none" in options
    assert "operator@10.0.0.10" in command
    assert command[command.index("-p") + 1] == "2222"
    assert Path(control_path).parent.stat().st_mode & 0o777 == 0o700
    assert Path(control_path).parent == tmp_path / ".homelab-state" / "cm"


def test_local_forward_uses_loopback_and_cleans_up(tmp_path, monkeypatch) -> None:
    cfg = Config(
        machines={
            "control": MachineSpec(
                hostname="control",
                address="10.0.0.10",
                gateway="10.0.0.1",
                vmid=800,
                labels=("control",),
            ),
            "apps": MachineSpec(
                hostname="apps",
                address="10.0.0.20",
                gateway="10.0.0.1",
                vmid=801,
                labels=("apps",),
                ssh_user="operator",
                ssh_port=2222,
            ),
        }
    )
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(ssh_transport, "_reserve_local_port", lambda: 43123, raising=False)
    monkeypatch.setattr(ssh_transport, "_wait_for_local_forward", MagicMock(), raising=False)
    monkeypatch.setattr(ssh_transport, "resolve_ansible_ssh_key", lambda *_a: tmp_path / "deploy-key")
    monkeypatch.setattr(ssh_transport, "ssh_proxy_command", lambda *_a: "ssh-jump")
    monkeypatch.setattr(ssh_transport.subprocess, "Popen", popen)

    with ssh_transport.ssh_local_forward(
        cfg,
        tmp_path,
        "10.0.0.20",
        8080,
        remote_host="10.0.0.20",
    ) as local_port:
        assert local_port == 43123
        process.poll.assert_called_with()

    command = popen.call_args.args[0]
    assert "ExitOnForwardFailure=yes" in command
    assert "ControlMaster=no" in command
    assert "127.0.0.1:43123:10.0.0.20:8080" in command
    assert command[-1] == "operator@10.0.0.20"
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once()
    process.kill.assert_not_called()


def test_local_forward_cleanup_does_not_mask_body_error(tmp_path, monkeypatch) -> None:
    cfg = Config(domain="example.com")
    process = MagicMock()
    process.poll.return_value = None
    process.terminate.side_effect = OSError("already gone")

    monkeypatch.setattr(ssh_transport, "_reserve_local_port", lambda: 43123)
    monkeypatch.setattr(ssh_transport, "_wait_for_local_forward", lambda *_args: None)
    monkeypatch.setattr(ssh_transport, "resolve_ansible_ssh_key", lambda *_args: tmp_path / "deploy-key")
    monkeypatch.setattr(ssh_transport, "ssh_proxy_command", lambda *_args: "ssh-jump")
    monkeypatch.setattr(ssh_transport.subprocess, "Popen", MagicMock(return_value=process))

    with pytest.raises(ValueError, match="sync failed"):
        with ssh_transport.ssh_local_forward(
            cfg,
            tmp_path,
            "10.10.10.12",
            8082,
            remote_host="10.10.10.12",
        ):
            raise ValueError("sync failed")


def test_local_forward_reaps_process_when_startup_is_cancelled(tmp_path, monkeypatch) -> None:
    cfg = Config(domain="example.com")
    process = MagicMock()
    process.poll.return_value = None

    monkeypatch.setattr(ssh_transport, "_reserve_local_port", lambda: 43123)
    monkeypatch.setattr(
        ssh_transport,
        "_wait_for_local_forward",
        MagicMock(side_effect=KeyboardInterrupt),
    )
    monkeypatch.setattr(ssh_transport, "resolve_ansible_ssh_key", lambda *_args: tmp_path / "deploy-key")
    monkeypatch.setattr(ssh_transport, "ssh_proxy_command", lambda *_args: "ssh-jump")
    monkeypatch.setattr(ssh_transport.subprocess, "Popen", MagicMock(return_value=process))

    with pytest.raises(KeyboardInterrupt):
        with ssh_transport.ssh_local_forward(
            cfg,
            tmp_path,
            "10.10.10.12",
            8082,
            remote_host="10.10.10.12",
        ):
            pass

    process.terminate.assert_called_once_with()
    process.wait.assert_called_once()
