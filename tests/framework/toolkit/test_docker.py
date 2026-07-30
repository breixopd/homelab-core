"""Tests for toolkit/core/docker.py"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from tests.helpers.machines import single_control_machines
from toolkit.core.compose.docker import (
    ContainerStatus,
    DockerCompose,
    compose_for_category,
    compose_for_root,
    compose_process_environment,
)
from toolkit.core.config.config import Config, ServicesConfig


@pytest.fixture
def compose_file(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text("version: '3'\nservices:\n  web:\n    image: nginx")
    return f


@pytest.fixture
def env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=bar")
    return f


@pytest.fixture
def docker_compose(compose_file, env_file):
    return DockerCompose(compose_file=compose_file, env_file=env_file)


def mock_run(stdout="", stderr="", returncode=0):
    proc = Mock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_compose_process_environment_removes_ambient_env_file_overrides(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "")
    monkeypatch.setenv("UNRELATED", "preserved")

    env = compose_process_environment(env_file)

    assert "FOO" not in env
    assert env["UNRELATED"] == "preserved"


def test_compose_process_environment_applies_explicit_overrides(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "ambient")

    env = compose_process_environment(env_file, overrides={"FOO": "explicit"})

    assert env["FOO"] == "explicit"


def test_compose_for_root_uses_role_model_for_multi_node_config(tmp_path: Path) -> None:
    role_compose = tmp_path / "generated" / "media" / "compose.yaml"
    role_compose.parent.mkdir(parents=True)
    role_compose.write_text("name: homelab\nservices: {}\n", encoding="utf-8")

    selected = compose_for_root(Config(), tmp_path, vm="media")

    assert selected is not None
    assert selected.compose_file == role_compose


def test_compose_for_root_does_not_fall_back_to_full_model_on_multi_node(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("name: homelab\nservices: {}\n", encoding="utf-8")

    assert compose_for_root(Config(), tmp_path, vm="media") is None


def test_compose_for_root_uses_full_model_for_single_node_config(tmp_path: Path) -> None:
    root_compose = tmp_path / "docker-compose.yml"
    root_compose.write_text("name: homelab\nservices: {}\n", encoding="utf-8")
    cfg = Config(
        services=ServicesConfig(media=False, cloud=False, email=False),
        machines=single_control_machines(),
    )

    selected = compose_for_root(cfg, tmp_path, vm="media")

    assert selected is not None
    assert selected.compose_file == root_compose


def test_compose_for_category_never_falls_back_to_full_multi_node_model(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("name: homelab\nservices: {}\n", encoding="utf-8")
    category = Mock(compose_file="docker-compose.yml")
    category.runtime_node.return_value = "media"

    with pytest.raises(FileNotFoundError, match="generated/media/compose.yaml"):
        compose_for_category(category, Config(), tmp_path)


class TestRun:
    def test_run_check_false_returns_completed_process(self, docker_compose):
        completed = mock_run(stdout="ok", returncode=0)
        with patch("subprocess.run", return_value=completed):
            result = docker_compose._run(["ps"])
        assert result.stdout == "ok"
        assert result.returncode == 0

    def test_run_check_false_preserves_nonzero_returncode(self, docker_compose):
        completed = mock_run(stdout="error", stderr="something failed", returncode=1)
        with patch("subprocess.run", return_value=completed):
            result = docker_compose._run(["up"])
        assert result.returncode == 1
        assert result.stdout == "error"

    def test_run_check_true_raises_on_nonzero(self, docker_compose):
        err = subprocess.CalledProcessError(1, "docker compose")
        with patch("subprocess.run", side_effect=err):
            with pytest.raises(subprocess.CalledProcessError) as exc:
                docker_compose._run(["pull"], check=True)
            assert exc.value.returncode == 1

    def test_run_timeout_passed_correctly(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run()
            docker_compose._run(["ps"], timeout=60)
            _, kwargs = mock_run_fn.call_args
            assert kwargs["timeout"] == 60

    def test_run_capture_output_true_by_default(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run()
            docker_compose._run(["ps"])
            _, kwargs = mock_run_fn.call_args
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True


class TestPreflight:
    def test_preflight_returns_true_when_docker_running(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            result = docker_compose.preflight()
        assert result is True

    def test_preflight_returns_false_when_docker_unreachable(self, docker_compose):
        completed = mock_run(stderr="Cannot connect to daemon", returncode=1)
        with patch("subprocess.run", return_value=completed):
            result = docker_compose.preflight()
        assert result is False

    def test_preflight_returns_false_on_timeout(self, docker_compose):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 15)):
            result = docker_compose.preflight()
        assert result is False

    def test_preflight_returns_false_when_docker_not_found(self, docker_compose):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = docker_compose.preflight()
        assert result is False

    def test_preflight_timeout_passed_correctly(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.preflight(timeout=30)
            _, kwargs = mock_run_fn.call_args
            assert kwargs["timeout"] == 30


class TestUp:
    def test_up_returns_true_on_success(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            result = docker_compose.up()
        assert result is True

    def test_up_returns_false_on_failure(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=1)):
            result = docker_compose.up()
        assert result is False

    def test_up_with_detach_flag(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.up(detach=True)
            args, _ = mock_run_fn.call_args
            assert "-d" in args[0]

    def test_up_without_detach_flag(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.up(detach=False)
            args, _ = mock_run_fn.call_args
            assert "-d" not in args

    def test_up_with_profiles(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.up(profiles=["media", "apps"])
            args, _ = mock_run_fn.call_args
            assert "--profile" in args[0]
            assert "media" in args[0]
            assert "apps" in args[0]

    def test_up_with_services(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.up(services=["web", "redis"])
            args, _ = mock_run_fn.call_args
            assert "web" in args[0]
            assert "redis" in args[0]


class TestDown:
    def test_down_returns_true_on_success(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            result = docker_compose.down()
        assert result is True

    def test_down_returns_false_on_failure(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=1)):
            result = docker_compose.down()
        assert result is False

    def test_down_with_remove_volumes(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.down(remove_volumes=True)
            args, _ = mock_run_fn.call_args
            assert "-v" in args[0]

    def test_down_without_remove_volumes(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.down(remove_volumes=False)
            args, _ = mock_run_fn.call_args
            assert "-v" not in args


class TestPull:
    def test_pull_returns_true_on_success(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            result = docker_compose.pull()
        assert result is True

    def test_pull_returns_false_on_failure(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=1)):
            result = docker_compose.pull()
        assert result is False

    def test_pull_with_profiles(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.pull(profiles=["media"])
            args, _ = mock_run_fn.call_args
            assert "--profile" in args[0]

    def test_pull_with_services(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.pull(services=["web"])
            args, _ = mock_run_fn.call_args
            assert "web" in args[0]


class TestPs:
    def test_ps_parses_valid_json_lines(self, docker_compose):
        line1 = json.dumps(
            {
                "Name": "web-1",
                "Service": "web",
                "State": "running",
                "Health": "healthy",
                "Image": "nginx:latest",
            }
        )
        line2 = json.dumps(
            {
                "Name": "redis-1",
                "Service": "redis",
                "State": "exited",
                "Health": "",
                "Image": "redis:alpine",
            }
        )
        stdout = f"{line1}\n{line2}\n"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            containers = docker_compose.ps()

        assert len(containers) == 2
        assert containers[0].name == "web-1"
        assert containers[0].service == "web"
        assert containers[0].state == "running"
        assert containers[0].health == "healthy"
        assert containers[1].name == "redis-1"
        assert containers[1].service == "redis"
        assert containers[1].state == "exited"

    def test_ps_skips_empty_lines(self, docker_compose):
        stdout = json.dumps(
            {
                "Name": "web-1",
                "Service": "web",
                "State": "running",
                "Health": "",
                "Image": "nginx:latest",
            }
        )
        stdout += "\n\n\n"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            containers = docker_compose.ps()

        assert len(containers) == 1

    def test_ps_malformed_json_returns_empty_list(self, docker_compose):
        stdout = "this is not json\n{{invalid"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            containers = docker_compose.ps()

        assert containers == []

    def test_ps_json_decode_error_returns_empty_list(self, docker_compose):
        stdout = "valid line\n{invalid json"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            containers = docker_compose.ps()

        assert containers == []

    def test_ps_called_process_error_returns_empty_list(self, docker_compose):
        err = subprocess.CalledProcessError(1, "docker compose")
        with patch("subprocess.run", side_effect=err):
            containers = docker_compose.ps()

        assert containers == []

    def test_ps_missing_fields_uses_defaults(self, docker_compose):
        stdout = json.dumps({"Name": "web-1"})
        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            containers = docker_compose.ps()

        assert len(containers) == 1
        assert containers[0].name == "web-1"
        assert containers[0].service == ""
        assert containers[0].state == "unknown"
        assert containers[0].health == ""
        assert containers[0].image == ""

    def test_ps_empty_output_returns_empty_list(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(stdout="", returncode=0)):
            containers = docker_compose.ps()

        assert containers == []


class TestImageDigests:
    def test_image_digests_returns_service_to_image_map(self, docker_compose):
        line1 = json.dumps(
            {
                "Name": "web-1",
                "Service": "web",
                "State": "running",
                "Health": "",
                "Image": "nginx@sha256:abc123",
            }
        )
        line2 = json.dumps(
            {
                "Name": "redis-1",
                "Service": "redis",
                "State": "running",
                "Health": "",
                "Image": "redis@sha256:def456",
            }
        )
        stdout = f"{line1}\n{line2}\n"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            digests = docker_compose.image_digests()

        assert digests == {"web": "nginx@sha256:abc123", "redis": "redis@sha256:def456"}

    def test_image_digests_skips_containers_without_service(self, docker_compose):
        line1 = json.dumps(
            {
                "Name": "web-1",
                "Service": "",
                "State": "running",
                "Health": "",
                "Image": "nginx:latest",
            }
        )
        stdout = f"{line1}\n"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            digests = docker_compose.image_digests()

        assert digests == {}

    def test_image_digests_skips_containers_without_image(self, docker_compose):
        line1 = json.dumps(
            {
                "Name": "web-1",
                "Service": "web",
                "State": "running",
                "Health": "",
                "Image": "",
            }
        )
        stdout = f"{line1}\n"

        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            digests = docker_compose.image_digests()

        assert digests == {}

    def test_image_digests_empty_when_no_containers(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(stdout="", returncode=0)):
            digests = docker_compose.image_digests()

        assert digests == {}


class TestLogs:
    def test_logs_returns_combined_output(self, docker_compose):
        completed = mock_run(stdout="log line 1\n", stderr="error line\n", returncode=0)
        with patch("subprocess.run", return_value=completed):
            result = docker_compose.logs()
        assert "log line 1" in result
        assert "error line" in result

    def test_logs_returns_empty_on_called_process_error(self, docker_compose):
        err = subprocess.CalledProcessError(1, "docker compose")
        with patch("subprocess.run", side_effect=err):
            result = docker_compose.logs()
        assert result == ""

    def test_logs_with_services_filter(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(stdout="", returncode=0)
            docker_compose.logs(services=["web", "db"])
            args, _ = mock_run_fn.call_args
            assert "web" in args[0]
            assert "db" in args[0]

    def test_logs_with_tail_argument(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(stdout="", returncode=0)
            docker_compose.logs(tail=50)
            args, _ = mock_run_fn.call_args
            assert "--tail" in args[0]
            assert "50" in args[0]

    def test_logs_follow_mode(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(stdout="", returncode=0)
            docker_compose.logs(follow=True)
            args, _ = mock_run_fn.call_args
            assert "-f" in args[0]


class TestRestart:
    def test_restart_returns_true_on_success(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            result = docker_compose.restart()
        assert result is True

    def test_restart_returns_false_on_failure(self, docker_compose):
        with patch("subprocess.run", return_value=mock_run(returncode=1)):
            result = docker_compose.restart()
        assert result is False

    def test_restart_with_services(self, docker_compose):
        with patch("subprocess.run") as mock_run_fn:
            mock_run_fn.return_value = mock_run(returncode=0)
            docker_compose.restart(services=["web"])
            args, _ = mock_run_fn.call_args
            assert "web" in args[0]


class TestBaseCmd:
    def test_base_cmd_includes_docker_compose(self, docker_compose):
        cmd = docker_compose._base_cmd()
        assert cmd[0] == "docker"
        assert cmd[1] == "compose"

    def test_base_cmd_includes_compose_file_flag(self, docker_compose):
        cmd = docker_compose._base_cmd()
        assert "-f" in cmd
        assert str(docker_compose.compose_file) in cmd

    def test_base_cmd_includes_env_file_flag(self, docker_compose):
        cmd = docker_compose._base_cmd()
        assert "--env-file" in cmd
        assert str(docker_compose.env_file) in cmd

    def test_base_cmd_without_env_file(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("version: '3'")
        dc = DockerCompose(compose_file=compose_file, env_file=None)
        cmd = dc._base_cmd()
        assert "--env-file" not in cmd

    def test_base_cmd_includes_project_name(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("version: '3'")
        dc = DockerCompose(compose_file=compose_file, project_name="myproject")
        cmd = dc._base_cmd()
        assert "-p" in cmd
        assert "myproject" in cmd


class TestContainerStatus:
    def test_container_status_fields(self):
        cs = ContainerStatus(
            name="web-1",
            service="web",
            state="running",
            health="healthy",
            image="nginx:latest",
        )
        assert cs.name == "web-1"
        assert cs.service == "web"
        assert cs.state == "running"
        assert cs.health == "healthy"
        assert cs.image == "nginx:latest"

    def test_container_status_default_values(self):
        cs = ContainerStatus(
            name="web-1",
            service="web",
            state="running",
            health="",
            image="nginx:latest",
        )
        assert cs.health == ""
