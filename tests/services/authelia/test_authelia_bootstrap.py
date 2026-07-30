from __future__ import annotations

from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config
from toolkit.services.authelia.bootstrap import reset_authelia_storage


def test_storage_reset_keeps_database_password_and_sql_off_process_arguments(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.core.config.config.load_config", lambda _path: Config())
    monkeypatch.setattr("toolkit.core.manifest.placement.service_node", lambda _cfg, _service: "infra")
    monkeypatch.setattr(
        "toolkit.services.authelia.bootstrap.load_env_file",
        lambda _path: {
            "POSTGRES_USER": "admin",
            "POSTGRES_PASSWORD": "postgres-test-password",
            "AUTHELIA_DB_PASSWORD": "authelia-test-password",
        },
    )

    with (
        patch("toolkit.services.authelia.bootstrap.docker_exec", return_value=(0, "")) as docker_exec,
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
    ):
        logs = reset_authelia_storage(tmp_path, docker_bin="docker-probe")

    assert logs[-2:] == [
        "Authelia: recreated authelia database (encryption key resync)",
        "Authelia: container restarted",
    ]
    assert docker_exec.call_count == 4
    for call in docker_exec.call_args_list:
        command = call.args[1]
        assert "postgres-test-password" not in " ".join(command)
        assert "authelia-test-password" not in " ".join(command)
        assert "-c" not in command
        assert call.kwargs["docker_bin"] == "docker-probe"
        assert call.kwargs["secret_environment"] == {"PGPASSWORD": "postgres-test-password"}
        assert call.kwargs["stdin"].endswith("\n")
