"""PostgreSQL database reconciliation fail-gates and contract coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, ProjectEntry, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.services.sdk.postgres import (
    PostgresReconcileResult,
    ensure_postgres_healthy,
    reconcile_service_databases,
    sync_project_postgres_databases,
)

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _write_env(path: Path, **values: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")


def _mock_psql_side_effect(*, fail_user: str | None = None):
    def _run(_container, command, *args, **kwargs):
        assert command[:4] == ["psql", "-v", "ON_ERROR_STOP=1", "-U"]
        assert "-c" not in command
        sql = kwargs["stdin"]
        if fail_user and fail_user in sql and ("ALTER USER" in sql or "CREATE USER" in sql):
            return 1, f'ERROR:  role "{fail_user}" does not exist'
        if "CREATE DATABASE" in sql:
            return 1, "ERROR:  database already exists"
        return 0, ""

    return _run


def test_sync_reports_failure_when_alter_fails(tmp_path: Path) -> None:
    env_file = tmp_path / "generated" / "infra" / ".env"
    _write_env(
        env_file,
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
        GITEA_DB_PASSWORD="gitea-pass",
        NEXTCLOUD_DB_PASSWORD="nc-pass",
        AUTHELIA_DB_PASSWORD="auth-pass",
        GRAFANA_DB_PASSWORD="graf-pass",
        VAULTWARDEN_DB_PASSWORD="vw-pass",
        ROMM_DB_PASSWORD="romm-pass",
    )

    side_effect = _mock_psql_side_effect(fail_user="gitea")
    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=side_effect):
        result = reconcile_service_databases(tmp_path, node="infra")

    assert isinstance(result, PostgresReconcileResult)
    assert result.success is False
    assert "gitea" in result.failed_roles
    assert any("database reconciliation FAILED" in line for line in result.logs)
    assert result.roles["gitea"] == "failed"
    assert result.roles["nextcloud"] == "ok"


def test_sync_success_when_all_users_ok(tmp_path: Path) -> None:
    env_file = tmp_path / "generated" / "infra" / ".env"
    _write_env(
        env_file,
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
        GITEA_DB_PASSWORD="gitea-pass",
        NEXTCLOUD_DB_PASSWORD="nc-pass",
        AUTHELIA_DB_PASSWORD="auth-pass",
        GRAFANA_DB_PASSWORD="graf-pass",
        VAULTWARDEN_DB_PASSWORD="vw-pass",
        ROMM_DB_PASSWORD="romm-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()):
        result = reconcile_service_databases(tmp_path, node="infra")

    assert result.success is True
    assert not result.failed_roles
    assert all(status == "ok" for status in result.roles.values())
    assert "immich" not in result.roles


def test_sync_discovers_database_roles_from_consumer_manifests(tmp_path: Path) -> None:
    provider = ServiceManifest.model_validate(
        {
            "name": "database",
            "label": "Database",
            "description": "Custom PostgreSQL provider",
            "icon": "database",
            "category": "cloud",
            "placement": "apps",
            "priority": 10,
            "variables": {"DATABASE_USER": "root", "DATABASE_NAME": "postgres"},
            "required_secrets": [
                {
                    "name": "DATABASE_PASSWORD",
                    "tier": "generated",
                    "description": "administrator password",
                    "rotation": "persistent",
                }
            ],
            "database_provider": {
                "engine": "postgresql",
                "admin_username_env": "DATABASE_USER",
                "admin_password_env": "DATABASE_PASSWORD",
                "admin_database_env": "DATABASE_NAME",
            },
            "service_endpoint": {"container_port": 5432},
        }
    )
    consumer = ServiceManifest.model_validate(
        {
            "name": "application",
            "label": "Application",
            "description": "Custom database consumer",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 20,
            "depends_on": ["database"],
            "required_secrets": [
                {
                    "name": "APPLICATION_DB_PASSWORD",
                    "tier": "generated",
                    "description": "database password",
                    "rotation": "reconcile",
                }
            ],
            "databases": [
                {
                    "provider": "database",
                    "database": "application_data",
                    "username": "application_user",
                    "host_env": "APPLICATION_DB_HOST",
                    "port_env": "APPLICATION_DB_PORT",
                    "database_env": "APPLICATION_DB_NAME",
                    "username_env": "APPLICATION_DB_USER",
                    "password_env": "APPLICATION_DB_PASSWORD",
                }
            ],
        }
    )
    catalog = ServiceCatalog((provider, consumer))
    _write_env(
        tmp_path / "generated" / "apps" / ".env",
        DATABASE_USER="root",
        DATABASE_PASSWORD="root-pass",
        DATABASE_NAME="postgres",
        APPLICATION_DB_PASSWORD="app-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()) as run:
        result = reconcile_service_databases(
            tmp_path,
            node="apps",
            provider="database",
            cfg=Config(services={"cloud": True}),
            catalog=catalog,
        )

    assert result.success is True
    assert result.roles == {"application_user": "ok"}
    sql_commands = [call.kwargs["stdin"] for call in run.call_args_list]
    assert any('CREATE USER "application_user"' in sql for sql in sql_commands)
    assert any('CREATE DATABASE "application_data" OWNER "application_user"' in sql for sql in sql_commands)
    assert any('ALTER DATABASE "application_data" OWNER TO "application_user"' in sql for sql in sql_commands)
    assert not any("gitea" in sql for sql in sql_commands)


def test_sync_fails_when_database_creation_returns_an_unexpected_error(tmp_path: Path) -> None:
    _write_env(
        tmp_path / "generated" / "infra" / ".env",
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
        GITEA_DB_PASSWORD="gitea-pass",
        NEXTCLOUD_DB_PASSWORD="nc-pass",
        AUTHELIA_DB_PASSWORD="auth-pass",
        GRAFANA_DB_PASSWORD="graf-pass",
        VAULTWARDEN_DB_PASSWORD="vw-pass",
        ROMM_DB_PASSWORD="romm-pass",
    )

    def fail_create(_container, _command, *args, **kwargs):
        if "CREATE DATABASE" in kwargs["stdin"]:
            return 1, "ERROR: permission denied"
        return 0, ""

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=fail_create):
        result = reconcile_service_databases(tmp_path, node="infra")

    assert result.success is False
    assert all(status == "failed" for status in result.roles.values())
    assert any("create failed" in line and "permission denied" in line for line in result.logs)


def test_sync_fails_users_without_required_password_environment(tmp_path: Path) -> None:
    env_file = tmp_path / "generated" / "infra" / ".env"
    _write_env(
        env_file,
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
        GITEA_DB_PASSWORD="gitea-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()):
        result = reconcile_service_databases(tmp_path, node="infra")

    assert result.success is False
    assert result.roles["gitea"] == "ok"
    assert result.roles["nextcloud"] == "failed"
    assert "headscale" not in result.roles


def test_health_restarts_a_missing_provider_container(tmp_path: Path) -> None:
    _write_env(
        tmp_path / "generated" / "infra" / ".env",
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
    )
    inspect = MagicMock(returncode=1, stdout="", stderr="missing")
    started = MagicMock(returncode=0, stdout="started", stderr="")
    ready = MagicMock(returncode=0, stdout="accepting connections", stderr="")

    with patch(
        "toolkit.services.sdk.postgres.subprocess.run",
        side_effect=[inspect, started, ready],
    ) as run:
        logs = ensure_postgres_healthy(tmp_path, node="infra", sync_passwords=False)

    assert any("missing" in line and "starting" in line for line in logs)
    assert run.call_args_list[1].args[0][-3:] == ["up", "-d", "postgres"]


def test_health_probe_can_defer_password_sync_to_caller(tmp_path: Path) -> None:
    _write_env(tmp_path / "generated" / "infra" / ".env", POSTGRES_USER="admin")

    with (
        patch(
            "toolkit.services.sdk.postgres.subprocess.run",
            side_effect=lambda *_a, **_k: MagicMock(returncode=0, stdout="accepting connections", stderr=""),
        ),
        patch("toolkit.services.sdk.postgres.reconcile_service_databases") as reconcile,
    ):
        logs = ensure_postgres_healthy(tmp_path, node="infra", sync_passwords=False)

    assert any("accepting connections" in line for line in logs)
    reconcile.assert_not_called()


def test_dev_sync_creates_project_role_and_database(tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="status",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=PINNED_IMAGE,
            placement="apps",
            database_service="dev-postgres",
        )
    ]
    save_config(cfg, config_path(tmp_path))
    _write_env(
        tmp_path / "generated" / "apps" / ".env",
        DEV_POSTGRES_USER="dev",
        DEV_POSTGRES_PASSWORD="admin-pass",
        DEV_POSTGRES_DB="dev",
        STATUS_POSTGRES_PASSWORD="status-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()) as run:
        result = sync_project_postgres_databases(tmp_path, provider="dev-postgres", cfg=cfg)

    assert result.success is True
    assert result.roles == {"status": "ok"}
    sql_commands = [call.kwargs["stdin"] for call in run.call_args_list]
    assert any('CREATE USER "status"' in sql for sql in sql_commands)
    assert any('CREATE DATABASE "status" OWNER "status"' in sql for sql in sql_commands)


def test_dev_sync_quotes_sanitized_project_database_identifiers(tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="team-status",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=PINNED_IMAGE,
            placement="apps",
            database_service="dev-postgres",
        )
    ]
    save_config(cfg, config_path(tmp_path))
    _write_env(
        tmp_path / "generated" / "apps" / ".env",
        DEV_POSTGRES_USER="dev",
        DEV_POSTGRES_PASSWORD="admin-pass",
        DEV_POSTGRES_DB="dev",
        TEAM_STATUS_POSTGRES_PASSWORD="status-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()) as run:
        result = sync_project_postgres_databases(tmp_path, provider="dev-postgres", cfg=cfg)

    assert result.success is True
    sql_commands = [call.kwargs["stdin"] for call in run.call_args_list]
    assert any('CREATE USER "team_status"' in sql for sql in sql_commands)
    assert any('CREATE DATABASE "team_status" OWNER "team_status"' in sql for sql in sql_commands)


def test_reconciliation_keeps_passwords_and_sql_off_process_arguments(tmp_path: Path) -> None:
    _write_env(
        tmp_path / "generated" / "infra" / ".env",
        POSTGRES_USER="admin",
        POSTGRES_PASSWORD="admin-pass",
        POSTGRES_DB="postgres",
        GITEA_DB_PASSWORD="gitea-pass",
        NEXTCLOUD_DB_PASSWORD="nc-pass",
        AUTHELIA_DB_PASSWORD="auth-pass",
        GRAFANA_DB_PASSWORD="graf-pass",
        VAULTWARDEN_DB_PASSWORD="vw-pass",
        ROMM_DB_PASSWORD="romm-pass",
    )

    with patch("toolkit.services.sdk.postgres.docker_exec", side_effect=_mock_psql_side_effect()) as run:
        result = reconcile_service_databases(tmp_path, node="infra")

    assert result.success
    for call in run.call_args_list:
        command = call.args[1]
        assert "admin-pass" not in repr(command)
        assert "gitea-pass" not in repr(command)
        assert "-c" not in command
        assert call.kwargs["secret_environment"] == {"PGPASSWORD": "admin-pass"}
        assert call.kwargs["stdin"].endswith("\n")
