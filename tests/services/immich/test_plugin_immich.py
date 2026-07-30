"""Unit tests for immich-server, immich-postgres, and immich-machine-learning verify()."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig

SERVICE_ROOT = Path(__file__).resolve().parents[3] / "toolkit" / "services" / "immich-server"


def _plugin(service: str):
    for name in dir(mod := load_plugin(service)):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError(f"no plugin for {service}")


class TestImmichServerVerify:
    def test_ping_and_version(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            if url.endswith("/ping"):
                return 0, json.dumps({"res": "pong"})
            if url.endswith("/version"):
                return 0, json.dumps({"major": 2, "minor": 7})
            return 255, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, "OK"),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.oidc_check_env_issuer",
            lambda *_a, **_k: [],
        )

        checks = {c.check: c for c in _plugin("immich-server").verify(cfg, {}, "10.10.10.12", tmp_path)}
        assert checks["ping"].passed
        assert checks["version"].passed
        assert checks["storage_writable"].passed
        assert checks["ml_ping"].passed


class TestImmichPostgresVerify:
    def test_pgvector_and_db(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        secrets = {"IMMICH_DB_PASSWORD": "pw"}
        calls = []

        def fake_exec(_cfg, container, cmd, _ip, _root, **kw):
            calls.append((container, cmd, kw))
            joined = " ".join(cmd)
            if "pg_database" in joined:
                return 0, "1"
            if "pg_extension" in joined:
                return 0, "vector\nvchord\nearthdistance"
            if "SELECT 1" in joined:
                return 0, "1"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {c.check: c for c in _plugin("immich-postgres").verify(cfg, secrets, "10.10.10.12", tmp_path)}
        assert checks["connect"].passed
        assert checks["database"].passed
        assert checks["extensions"].passed
        assert all("pw" not in " ".join(command) for _container, command, _kwargs in calls)
        assert all(kwargs["secret_environment"] == {"PGPASSWORD": "pw"} for _container, _command, kwargs in calls)

    def test_post_start_bootstraps_required_extensions_secret_safely(self, monkeypatch):
        plugin = _plugin("immich-postgres")
        calls = []

        def fake_exec(service, command, **kwargs):
            calls.append((service, command, kwargs))
            return 0, "CREATE EXTENSION\nCREATE EXTENSION\n"

        monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
        logs = plugin.post_start(
            Config(domain="example.com", services=ServicesConfig(cloud=True)),
            {
                "IMMICH_POSTGRES_ADMIN_USER": "postgres",
                "IMMICH_POSTGRES_ADMIN_PASSWORD": "admin-secret",
            },
        )

        assert logs == ["Immich PostgreSQL: vchord and earthdistance extensions ready"]
        command = calls[0][1]
        assert "admin-secret" not in " ".join(command)
        assert calls[0][2]["secret_environment"] == {"PGPASSWORD": "admin-secret"}
        assert "CREATE EXTENSION IF NOT EXISTS vchord" in calls[0][2]["stdin"]
        assert "CREATE EXTENSION IF NOT EXISTS earthdistance" in calls[0][2]["stdin"]


class TestImmichMlVerify:
    def test_ping(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "pong"))
        checks = _plugin("immich-machine-learning").verify(cfg, {}, "10.10.10.12", tmp_path)
        assert checks[0].passed
        assert checks[0].check == "ping"


def test_geodata_marker_binds_image_metadata_as_psql_variable():
    module = importlib.import_module("toolkit.services.immich-server.bootstrap")
    image_value = "2026-07-12$json$; DROP TABLE system_metadata; --"
    image_probe = MagicMock(returncode=0, stdout=image_value + "\n", stderr="")

    with (
        patch("subprocess.run", return_value=image_probe),
        patch("toolkit.core.ops.automation.docker_exec", return_value=(0, "")) as docker_exec,
    ):
        logs = module.mark_immich_geodata_import_complete({"IMMICH_DB_PASSWORD": "secret"})

    assert logs == [f"Immich geodata: import marker set (date={image_value})"]
    command = docker_exec.call_args.args[1]
    sql = docker_exec.call_args.kwargs["stdin"]
    assert image_value not in sql
    assert ":'payload'::jsonb" in sql
    assert f"payload={json.dumps({'lastUpdate': image_value, 'lastImportFileName': 'cities500.txt'})}" in command
    assert "-c" not in command
    assert docker_exec.call_args.kwargs["secret_environment"] == {"PGPASSWORD": "secret"}


def test_schema_repair_delivers_database_password_as_secret_environment() -> None:
    module = importlib.import_module("toolkit.services.immich-server.bootstrap")
    calls = []

    def fake_docker_exec(_service, command, **kwargs):
        calls.append((command, kwargs))
        return (0, "1") if "-tA" in command else (0, "")

    with patch("toolkit.core.ops.automation.docker_exec", side_effect=fake_docker_exec):
        logs = module.repair_immich_schema_drift({"IMMICH_DB_PASSWORD": "schema-test-password"})

    assert logs[-1] == "Immich schema: applied 6/6 drift fixes"
    assert all("schema-test-password" not in " ".join(command) for command, _kwargs in calls)
    assert all(kwargs["secret_environment"] == {"PGPASSWORD": "schema-test-password"} for _command, kwargs in calls)
    assert all("-c" not in command for command, _kwargs in calls)
    assert all(kwargs["stdin"] for command, kwargs in calls if "-tAc" not in command)


def test_manifest_declares_admin_bootstrap_password() -> None:
    manifest = yaml.safe_load((SERVICE_ROOT / "service.yaml").read_text())
    required = {entry["name"] for entry in manifest["required_secrets"]}

    assert "IMMICH_ADMIN_PASSWORD" in required


def test_immich_uses_dedicated_noeviction_redis_runtime() -> None:
    compose = yaml.safe_load((SERVICE_ROOT / "compose.yaml").read_text())
    redis = compose["services"]["immich-redis"]
    assert "noeviction" in redis["command"][-1]
    assert redis["container_name"] == "immich-redis"
    assert redis["read_only"] is True
    assert redis["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    assert compose["services"]["immich-server"]["environment"]["REDIS_HOSTNAME"] == "immich-redis"
    assert compose["services"]["immich-server"]["environment"]["REDIS_PASSWORD"] == "${IMMICH_REDIS_PASSWORD}"

    manifest = yaml.safe_load((SERVICE_ROOT / "service.yaml").read_text())
    assert "redis" not in manifest["depends_on"]
    assert "integrations" not in manifest
    assert "IMMICH_REDIS_PASSWORD" in {secret["name"] for secret in manifest["required_secrets"]}


def test_existing_admin_password_is_reconciled_with_official_server_command() -> None:
    module = importlib.import_module("toolkit.services.immich-server.bootstrap")
    cfg = Config(domain="example.com", email="admin@example.com")
    responses = [
        MagicMock(status_code=400),
        MagicMock(status_code=401),
        MagicMock(status_code=201),
    ]

    with (
        patch.object(module, "resolve_docker_service_url", return_value="http://immich"),
        patch.object(module.httpx, "get", return_value=MagicMock(status_code=200)),
        patch.object(module.httpx, "post", side_effect=responses),
        patch("toolkit.core.ops.automation.docker_exec", return_value=(0, "updated")) as docker_exec,
    ):
        logs = module.bootstrap_immich_admin(cfg, {"IMMICH_ADMIN_PASSWORD": "managed-password"})

    assert logs[-1] == "Immich: admin password reconciled and login verified"
    docker_exec.assert_called_once_with(
        "immich-server",
        ["immich-admin", "reset-admin-password"],
        stdin="managed-password\n",
        timeout=30,
    )


def test_oidc_configuration_accepts_immich_v2_login_status() -> None:
    module = importlib.import_module("toolkit.services.immich-server.bootstrap")
    cfg = Config(domain="example.com", email="admin@example.com")
    login = MagicMock(status_code=201)
    login.json.return_value = {"accessToken": "access-token"}
    current = MagicMock(status_code=200, content=b"{}")
    current.json.return_value = {}

    with (
        patch.object(module, "resolve_docker_service_url", return_value="http://immich"),
        patch.object(module.httpx, "post", return_value=login),
        patch.object(module.httpx, "get", return_value=current),
        patch.object(module.httpx, "put", return_value=MagicMock(status_code=200)) as put,
    ):
        logs = module.configure_immich_oidc(
            cfg,
            {
                "IMMICH_ADMIN_PASSWORD": "managed-password",
                "IMMICH_OIDC_CLIENT_SECRET": "client-secret",
            },
        )

    assert logs == ["Immich OIDC: OAuth enabled via system-config"]
    oauth = put.call_args.kwargs["json"]["oauth"]
    assert oauth["mobileOverrideEnabled"] is True
    assert oauth["mobileRedirectUri"] == "https://photos.example.com/api/oauth/mobile-redirect"


def test_manifest_registers_https_mobile_oauth_bridge() -> None:
    manifest = yaml.safe_load((SERVICE_ROOT / "service.yaml").read_text())

    assert "https://photos.{domain}/api/oauth/mobile-redirect" in manifest["oidc"]["redirect_uris"]


def test_prepare_runtime_deployment_creates_upload_integrity_markers(tmp_path: Path) -> None:
    class Context:
        root = tmp_path

        def environment(self, name: str, default: str = "") -> str:
            return str(tmp_path / "data" / "immich-upload") if name == "IMMICH_UPLOAD_SOURCE" else default

        def log(self, _message: str) -> None:
            pass

        def warn(self, _message: str) -> None:
            raise AssertionError("unexpected warning")

    _plugin("immich-server").prepare_runtime_deployment(Context(), ("immich-server",))

    upload = tmp_path / "data" / "immich-upload"
    assert all(
        (upload / name / ".immich").is_file()
        for name in ("thumbs", "encoded-video", "backups", "library", "profile", "upload")
    )
