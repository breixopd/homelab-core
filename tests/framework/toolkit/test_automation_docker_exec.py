from __future__ import annotations

from unittest.mock import MagicMock, patch

from toolkit.core.ops.automation import docker_curl, docker_exec


def test_docker_exec_pipes_stdin_without_putting_it_in_arguments():
    completed = MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("toolkit.core.ops.automation.run_text_process_group", return_value=completed) as run:
        rc, output = docker_exec("service", ["helper"], stdin="sensitive-json")

    assert (rc, output) == (0, "ok")
    assert run.call_args.args[0] == ["docker", "exec", "-i", "service", "helper"]
    assert run.call_args.kwargs["input_text"] == "sensitive-json"
    assert "sensitive-json" not in repr(run.call_args.args[0])


def test_docker_exec_delivers_secret_environment_over_stdin():
    completed = MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("toolkit.core.ops.automation.run_text_process_group", return_value=completed) as run:
        rc, output = docker_exec(
            "service",
            ["helper", "--check"],
            environment={"PUBLIC_MODE": "enabled"},
            secret_environment={"PGPASSWORD": "test-only-password"},
            stdin="SELECT 1;\n",
        )

    assert (rc, output) == (0, "ok")
    command = run.call_args.args[0]
    assert command[:7] == ["docker", "exec", "-i", "-e", "PUBLIC_MODE=enabled", "service", "sh"]
    assert "test-only-password" not in repr(command)
    assert command[-2:] == ["helper", "--check"]
    assert run.call_args.kwargs["input_text"].endswith("__HOMELAB_SECRET_ENV_END__\nSELECT 1;\n")
    assert "PGPASSWORD=test-only-password\n" in run.call_args.kwargs["input_text"]


def test_docker_curl_pipes_authenticated_request_config_without_secret_arguments():
    completed = MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("toolkit.core.ops.automation.run_text_process_group", return_value=completed) as run:
        rc, output = docker_curl(
            "service",
            "http://127.0.0.1:8080/api/login",
            method="POST",
            headers={"Authorization": "Bearer test-only-token"},
            body="password=test-only-password",
            cookie_jar="/tmp/session.cookies",
        )

    assert (rc, output) == (0, "ok")
    command = run.call_args.args[0]
    assert command[:4] == ["bash", "-o", "pipefail", "-c"]
    assert "docker exec -i service curl --disable --config - 2>&1 | head -c 8388609" in command[4]
    assert "test-only-token" not in repr(command)
    assert "test-only-password" not in repr(command)
    request = run.call_args.kwargs["input_text"]
    assert 'header = "Authorization: Bearer test-only-token"' in request
    assert 'data-raw = "password=test-only-password"' in request
    assert 'cookie-jar = "/tmp/session.cookies"' in request
    assert "max-filesize = 8388608" in request
    assert run.call_args.kwargs["timeout"] == 15


def test_docker_exec_rejects_oversized_output_at_source():
    completed = MagicMock(returncode=23, stdout="123456789", stderr="")

    with patch("toolkit.core.ops.automation.run_text_process_group", return_value=completed) as run:
        rc, output = docker_exec("service", ["helper"], max_output_bytes=8)

    assert (rc, output) == (1, "Command output exceeds configured byte limit")
    assert run.call_args.args[0][:4] == ["bash", "-o", "pipefail", "-c"]
    assert run.call_args.args[0][4].endswith("2>&1 | head -c 9")


def test_docker_exec_actually_caps_combined_output(tmp_path):
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text("#!/usr/bin/env bash\nprintf '€€€€'\nprintf 'error-output' >&2\n")
    fake_docker.chmod(0o700)

    rc, output = docker_exec("service", ["helper"], docker_bin=str(fake_docker), max_output_bytes=7)

    assert (rc, output) == (1, "Command output exceeds configured byte limit")
