from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import yaml
from toolkit.core.ansible.ansible_runner import run_playbook_streaming, run_playbook_sync


class _FakeProcess:
    pid = 1234

    def __init__(self, stdout: str = "ok\n", stderr: str = "", *, timeout: bool = False):
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.timeout = timeout
        self.communicate_calls = 0
        self.returncode = 0

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.timeout and self.communicate_calls == 1:
            import subprocess

            raise subprocess.TimeoutExpired(
                "ansible-playbook", timeout, output=self.stdout_text, stderr=self.stderr_text
            )
        return self.stdout_text, self.stderr_text


def test_extra_vars_use_owner_only_ephemeral_file_not_process_arguments(tmp_path: Path) -> None:
    playbook = tmp_path / "automation" / "ansible" / "playbook.yml"
    inventory = tmp_path / "automation" / "ansible" / "inventory.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")
    inventory.write_text("all: {}\n", encoding="utf-8")
    secret = "mesh-secret-enrollment-material"
    observed_path: Path | None = None

    def run(command, **_kwargs):
        nonlocal observed_path
        rendered = " ".join(str(part) for part in command)
        assert secret not in rendered
        file_argument = next(str(part) for part in command if str(part).startswith("@"))
        observed_path = Path(file_argument.removeprefix("@"))
        assert observed_path.stat().st_mode & 0o777 == 0o600
        assert yaml.safe_load(observed_path.read_text(encoding="utf-8")) == {
            "mesh_auth_key": secret,
            "mesh_url": "https://vpn.example.test",
        }
        return _FakeProcess()

    with (
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", side_effect=run),
    ):
        result = run_playbook_sync(
            tmp_path,
            playbook,
            inventory=inventory,
            extra_vars={
                "mesh_url": "https://vpn.example.test",
                "mesh_auth_key": secret,
            },
        )

    assert result.ok
    assert observed_path is not None
    assert not observed_path.exists()


def test_extra_vars_reject_invalid_ansible_variable_names(tmp_path: Path) -> None:
    playbook = tmp_path / "automation" / "ansible" / "playbook.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")

    with patch("toolkit.core.ansible.ansible_runner.subprocess.Popen") as run:
        result = run_playbook_sync(tmp_path, playbook, extra_vars={"unsafe-name": "value"})

    assert not result.ok
    assert result.logs == ["Invalid Ansible variable name: unsafe-name"]
    run.assert_not_called()


def test_service_owned_secrets_are_injected_only_through_ephemeral_vars(tmp_path: Path) -> None:
    playbook = tmp_path / "automation" / "ansible" / "playbook.yml"
    inventory = tmp_path / "automation" / "ansible" / "inventory.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")
    inventory.write_text("all: {}\n", encoding="utf-8")
    bind_password = "directory-ephemeral-bind-canary"

    def run(command, **_kwargs):
        rendered = " ".join(str(part) for part in command)
        assert bind_password not in rendered
        file_argument = next(str(part) for part in command if str(part).startswith("@"))
        protected_vars = Path(file_argument.removeprefix("@"))
        assert protected_vars.stat().st_mode & 0o777 == 0o600
        assert yaml.safe_load(protected_vars.read_text(encoding="utf-8")) == {
            "service_bind_password": bind_password,
        }
        return _FakeProcess()

    with (
        patch(
            "toolkit.core.ansible.ansible_runner.deployment_secret_variables",
            return_value={"service_bind_password": bind_password},
        ),
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", side_effect=run),
    ):
        result = run_playbook_sync(tmp_path, playbook, inventory=inventory)

    assert result.ok


def test_sync_timeout_terminates_runner_process_group(tmp_path: Path) -> None:
    playbook = tmp_path / "automation/ansible/playbook.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")
    process = _FakeProcess(timeout=True)
    with (
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", return_value=process),
        patch("toolkit.core.ansible.ansible_runner._terminate_process_group") as terminate,
    ):
        result = run_playbook_sync(tmp_path, playbook, timeout=0.01)

    assert result.returncode == 124
    assert any("timed out" in line for line in result.logs)
    terminate.assert_called_once_with(process.pid)


class _HangingStream:
    async def readline(self):
        await asyncio.Event().wait()


class _AsyncProcess:
    pid = 5678
    returncode = 0

    def __init__(self):
        self.stdout = _HangingStream()
        self.waited = False

    async def wait(self):
        self.waited = True


async def _run_streaming_timeout(root: Path, playbook: Path) -> int:
    return await run_playbook_streaming(
        root,
        playbook,
        playbook.parent / "inventory",
        lambda _line, **_kwargs: None,
        timeout=0.01,
    )


async def _run_streaming_cancelled(root: Path, playbook: Path) -> None:
    task = asyncio.create_task(
        run_playbook_streaming(
            root,
            playbook,
            playbook.parent / "inventory",
            lambda _line, **_kwargs: None,
            timeout=60,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("streaming runner cancellation was swallowed")


def test_streaming_timeout_terminates_runner_process_group(tmp_path: Path) -> None:
    playbook = tmp_path / "automation/ansible/playbook.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")
    process = _AsyncProcess()
    with (
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.asyncio.create_subprocess_exec", return_value=process),
        patch("toolkit.core.ansible.ansible_runner._terminate_process_group") as terminate,
    ):
        result = asyncio.run(_run_streaming_timeout(tmp_path, playbook))

    assert result == 124
    assert process.waited
    terminate.assert_called_once_with(process.pid)


def test_streaming_cancellation_terminates_runner_process_group(tmp_path: Path) -> None:
    playbook = tmp_path / "automation/ansible/playbook.yml"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("---\n- hosts: all\n", encoding="utf-8")
    process = _AsyncProcess()
    with (
        patch("toolkit.core.ansible.ansible_runner.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_runner.generated_extra_vars", return_value=[]),
        patch("toolkit.core.ansible.ansible_runner.asyncio.create_subprocess_exec", return_value=process),
        patch("toolkit.core.ansible.ansible_runner._terminate_process_group") as terminate,
    ):
        asyncio.run(_run_streaming_cancelled(tmp_path, playbook))

    assert process.waited
    terminate.assert_called_once_with(process.pid)
