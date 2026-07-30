"""Unit tests for postgres and dev-postgres plugin verify()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig, ServicesConfig

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _plugin(service: str):
    module = load_plugin(service)
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError(f"no plugin class in {service}")


class TestPostgresPostStart:
    def test_retries_bootstrap_then_reconciles_databases(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        plugin = _plugin("postgres")
        monkeypatch.setattr(plugin, "runtime_node", lambda _cfg: "infra")
        healthy_calls = iter(
            [
                ["Postgres not ready after first boot"],
                ["Postgres: healthy"],
            ]
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.ensure_postgres_healthy",
            lambda *_a, **_k: next(healthy_calls),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.reconcile_service_databases",
            lambda *_a, **_k: SimpleNamespace(success=True, logs=["Postgres: databases reconciled"]),
        )
        monkeypatch.setattr("time.sleep", lambda _seconds: None)

        logs = plugin.post_start(cfg, {"POSTGRES_PASSWORD": "secret"}, root=tmp_path)

        assert logs == [
            "Postgres not ready after first boot",
            "Postgres not ready - retry attempt 2/3 in 10s",
            "Postgres: healthy",
            "Postgres: databases reconciled",
        ]

    def test_database_reconciliation_failure_is_fatal(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        plugin = _plugin("postgres")
        monkeypatch.setattr(plugin, "runtime_node", lambda _cfg: "infra")
        monkeypatch.setattr(
            "toolkit.services.sdk.ensure_postgres_healthy",
            lambda *_a, **_k: ["Postgres: healthy"],
        )
        sync_result = SimpleNamespace(success=False, logs=["Postgres: gitea failed"], failure_message=lambda: "gitea")
        monkeypatch.setattr(
            "toolkit.services.sdk.reconcile_service_databases",
            lambda *_a, **_k: sync_result,
        )

        with pytest.raises(RuntimeError, match="gitea"):
            plugin.post_start(cfg, {}, root=tmp_path)

    def test_reconciles_projects_that_select_the_shared_provider(self, tmp_path, monkeypatch):
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(management=True),
            projects=ProjectsConfig(
                entries=[
                    ProjectEntry(
                        subdomain="status",
                        auth_mode="forward_auth",
                        exposure="private",
                        docker_image=PINNED_IMAGE,
                        placement="apps",
                        database_service="postgres",
                    )
                ]
            ),
        )
        plugin = _plugin("postgres")
        monkeypatch.setattr(plugin, "runtime_node", lambda _cfg: "infra")
        monkeypatch.setattr(
            "toolkit.services.sdk.ensure_postgres_healthy",
            lambda *_a, **_k: ["Postgres: healthy"],
        )
        ok = SimpleNamespace(success=True, logs=[])
        monkeypatch.setattr(
            "toolkit.services.sdk.reconcile_service_databases",
            lambda *_a, **_k: ok,
        )
        project_sync = patch(
            "toolkit.services.sdk.sync_project_postgres_databases",
            return_value=SimpleNamespace(success=True, logs=["Postgres: project synced"]),
        )

        with project_sync as sync:
            logs = plugin.post_start(cfg, {"POSTGRES_PASSWORD": "secret"}, root=tmp_path)

        assert logs[-1] == "Postgres: project synced"
        assert sync.call_args.kwargs["provider"] == "postgres"


class TestPostgresVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        checks = _plugin("postgres").verify(cfg, {"POSTGRES_PASSWORD": "x"}, "10.10.10.10", tmp_path)
        assert len(checks) == 1
        assert checks[0].passed
        assert "localhost" in checks[0].detail

    def test_user_connect_and_grants(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        secrets = {
            "POSTGRES_PASSWORD": "adminpass",
            "GITEA_DB_PASSWORD": "gp",
            "NEXTCLOUD_DB_PASSWORD": "np",
            "AUTHELIA_DB_PASSWORD": "ap",
            "GRAFANA_DB_PASSWORD": "grp",
            "VAULTWARDEN_DB_PASSWORD": "vp",
        }

        def fake_psql(_cfg, _ip, _root, *, user, database, password, sql, timeout=15):
            if sql.strip() == "SELECT 1":
                return (0, "1") if password else (1, "auth failed")
            if "rolcanlogin" in sql:
                return 0, ""
            if "has_database_privilege" in sql:
                return 0, "t"
            if "pg_get_userbyid" in sql:
                for expected in ("gitea", "nextcloud", "authelia", "grafana", "vaultwarden"):
                    if f"datname = '{expected}'" in sql:
                        return 0, expected
                return 1, "unknown database"
            if "pg_is_in_recovery" in sql:
                return 0, "f"
            return 1, "unexpected"

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        pg_mod = load_plugin("postgres")
        monkeypatch.setattr(pg_mod, "_psql", fake_psql)

        checks = {c.check: c for c in _plugin("postgres").verify(cfg, secrets, "10.10.10.10", tmp_path)}
        assert checks["user_gitea"].passed
        assert checks["grant_gitea"].passed
        assert checks["owner_gitea"].passed
        assert checks["wal_primary"].passed

    def test_psql_query_delivers_credentials_and_sql_over_stdin(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        module = load_plugin("postgres")
        calls = []

        def fake_exec(_cfg, container, command, _ip, _root, **kwargs):
            calls.append((container, command, kwargs))
            return 0, "1"

        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        rc, output = module._psql(
            cfg,
            "10.10.10.10",
            tmp_path,
            user="app",
            database="app",
            password="test-only-password",
            sql="SELECT 1",
        )

        assert (rc, output) == (0, "1")
        container, command, kwargs = calls[0]
        assert container == "postgres"
        assert "test-only-password" not in repr(command)
        assert "-c" not in command
        assert kwargs["secret_environment"] == {"PGPASSWORD": "test-only-password"}
        assert kwargs["stdin"] == "SELECT 1\n"


class TestDevPostgresVerify:
    def test_skips_missing_container(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin("dev-postgres").verify(cfg, {}, "10.10.10.12", tmp_path)
        assert checks[0].passed
        assert "skipped" in checks[0].detail

    def test_project_user_connect(self, tmp_path, monkeypatch):
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(cloud=True),
            projects=ProjectsConfig(
                entries=[
                    ProjectEntry(
                        subdomain="myapp",
                        auth_mode="forward_auth",
                        exposure="private",
                        docker_image=PINNED_IMAGE,
                        placement="apps",
                        database_service="dev-postgres",
                    )
                ]
            ),
        )

        def fake_exec(_cfg, container, cmd, _ip, _root, timeout=15, user="", **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "pg_isready" in joined:
                return 0, "accepting connections"
            if kwargs.get("stdin", "").strip() == "SELECT 1" and "myapp" in joined:
                return 0, "1"
            if kwargs.get("stdin", "").strip() == "SELECT 1":
                return 0, "1"
            return 1, "fail"

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        with patch("toolkit.core.config.config.config_path", return_value=tmp_path / "config.yaml"):
            with patch("toolkit.core.config.config.load_config", return_value=cfg):
                checks = {
                    c.check: c
                    for c in _plugin("dev-postgres").verify(
                        cfg,
                        {"DEV_POSTGRES_PASSWORD": "dev", "MYAPP_POSTGRES_PASSWORD": "secret"},
                        "10.10.10.12",
                        tmp_path,
                    )
                }
        assert checks["ready"].passed
        assert checks["user_myapp"].passed
