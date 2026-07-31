"""Unit tests for nextcloud plugin verify()."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    for name in dir(mod := load_plugin("nextcloud")):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError("no plugin")


def test_cron_sidecar_shares_nextcloud_data_volume():
    compose = yaml.safe_load((Path(__file__).parents[3] / "toolkit/services/nextcloud/compose.yaml").read_text())
    sidecar = compose["services"]["nextcloud-cron"]
    app = compose["services"]["nextcloud"]
    assert sidecar["command"] == "/cron.sh"
    assert sidecar["volumes"][0] == app["volumes"][0]


def test_apache_server_name_config_is_service_owned_and_read_only():
    service_root = Path(__file__).parents[3] / "toolkit/services/nextcloud"
    compose = yaml.safe_load((service_root / "compose.yaml").read_text())
    volumes = compose["services"]["nextcloud"]["volumes"]

    assert (
        "${INSTALL_ROOT}/toolkit/services/nextcloud/apache-server-name.conf:"
        "/etc/apache2/conf-enabled/server-name.conf:ro"
    ) in volumes
    assert (service_root / "apache-server-name.conf").read_text(encoding="utf-8") == "ServerName localhost\n"


class TestNextcloudVerify:
    def test_redis_probe_failure_cannot_pass(self, tmp_path):
        plugin = _plugin()

        def fake_occ(*_args, **_kwargs):
            command = next((arg for arg in _args if isinstance(arg, list)), [])
            if command[-2:] == ["redis:command", "PING"]:
                return 1, "connection refused"
            return 0, "installed: true\nmaintenance: false\nneedsDbUpgrade: false"

        check = plugin._check_db_redis(Config(), "10.10.10.12", tmp_path, fake_occ)
        assert check.passed is False

    def test_skips_when_cloud_off(self, tmp_path):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=False))
        checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)
        assert checks[0].passed
        assert "cloud not enabled" in checks[0].detail

    def test_full_verify(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        status = {"installed": True, "maintenance": False, "version": "33.0.5"}

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            if url.endswith("status.php"):
                return 0, json.dumps(status)
            return 255, ""

        occ_calls: list[list[str]] = []

        def fake_exec(_cfg, container, cmd, _ip, _root, **kw):
            args = cmd[2:] if cmd[:2] == ["php", "occ"] else cmd
            occ_calls.append(args)
            joined = " ".join(args)
            if "status" in joined and "db:" not in joined:
                return 0, "installed: true\nmaintenance: false\nneedsDbUpgrade: false"
            if "redis:command" in joined:
                return 0, "PONG"
            if "user_oidc:provider" in joined:
                return 0, "authelia https://auth.example.com"
            if "backgroundjobs_mode" in joined:
                return 0, "cron"
            if "lastcron" in joined:
                return 0, str(int(time.time()) - 60)
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)}
        assert checks["status"].passed
        assert checks["occ_status"].passed
        assert checks["db_redis"].passed
        assert checks["oidc_provider"].passed
        assert checks["cron_mode"].passed
        assert checks["last_cron"].passed
        assert ["config:app:get", "core", "lastcron"] in occ_calls

    def test_post_start_stops_after_bounded_admin_readiness_failure(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", email="admin@example.com", services=ServicesConfig(cloud=True))
        plugin = _plugin()
        monkeypatch.setattr("toolkit.core.ops.automation.health_check_logs", lambda _checks: [])
        monkeypatch.setattr(
            "toolkit.services.nextcloud.bootstrap.bootstrap_nextcloud_admin",
            lambda _cfg, _secrets: ["Nextcloud: occ not ready after 5min (connection refused)"],
        )

        def unexpected_follow_up(*_args, **_kwargs):
            raise AssertionError("follow-up bootstrap must not repeat the readiness wait")

        monkeypatch.setattr(
            "toolkit.services.nextcloud.bootstrap.configure_nextcloud_trusted_domain",
            unexpected_follow_up,
        )
        assert plugin.post_start(cfg, {}, root=tmp_path) == [
            "Nextcloud: occ not ready after 5min (connection refused)",
        ]
