"""Unit tests for gitea plugin verify()."""

from __future__ import annotations

import json
from unittest.mock import patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    for name in dir(mod := load_plugin("gitea")):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError("no plugin")


class TestGiteaVerify:
    def test_healthz_and_admin_api(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        secrets = {"GITEA_ADMIN_TOKEN": "tok123"}
        health = {
            "status": "pass",
            "checks": {"database:ping": [{"status": "pass"}]},
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            if "healthz" in url:
                return 0, json.dumps(health)
            if "admin/users" in url:
                return 0, '[{"login":"gitadmin"}]'
            return 255, ""

        def fake_exec(_cfg, container, cmd, _ip, _root, **kw):
            joined = " ".join(cmd)
            if "ENABLE_REVERSE_PROXY_AUTHENTICATION" in joined:
                return 0, "true"
            if "DISABLE_SSH" in joined:
                return 0, "true"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        monkeypatch.setattr(
            "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
            lambda *_a, **_k: (0, "HTTP/1.1 302 Found\nLocation: https://auth.example.com/\n", ""),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, secrets, "10.10.10.12", tmp_path)}
        assert checks["healthz"].passed
        assert checks["admin_api"].passed
        assert checks["oidc_auth"].passed
        assert checks["ssh_port"].passed
        assert checks["forward_auth"].passed

    def test_post_start_uses_admin_bootstrap(self, tmp_path):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

        with (
            patch("toolkit.core.ops.automation.health_check_logs", return_value=[]),
            patch(
                "toolkit.services.gitea.bootstrap.bootstrap_gitea_admin",
                return_value=["Gitea: admin ready"],
            ) as bootstrap,
        ):
            logs = _plugin().post_start(cfg, {}, root=tmp_path)

        bootstrap.assert_called_once_with(cfg, {}, root=tmp_path)
        assert logs == ["Gitea: admin ready"]

    def test_runtime_credentials_reconcile_is_service_owned(self, tmp_path):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        with patch(
            "toolkit.services.gitea.bootstrap.reconcile_gitea_runtime_credentials",
            return_value=["Gitea: controller admin token provisioned"],
        ) as reconcile:
            logs = _plugin().reconcile_runtime_credentials(cfg, tmp_path)

        reconcile.assert_called_once_with(cfg, tmp_path)
        assert logs == ["Gitea: controller admin token provisioned"]

    def test_admin_api_requires_the_bootstrapped_token(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, '{"status":"pass","checks":{}}'))
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (1, ""))
        monkeypatch.setattr(
            "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
            lambda *_a, **_k: (0, "HTTP/1.1 302 Found\nLocation: https://auth.example.com/\n", ""),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)}

        assert checks["admin_api"].passed is False
        assert checks["admin_api"].detail == "GITEA_ADMIN_TOKEN not set"

    def test_healthz_new_database_ping_format(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        health = {
            "status": "pass",
            "checks": {
                "database:ping": [{"status": "pass", "time": "2026-07-02T01:29:42Z"}],
                "cache:ping": [{"status": "pass", "time": "2026-07-02T01:29:42Z"}],
            },
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            if "healthz" in url:
                return 0, json.dumps(health)
            return 255, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (1, ""),
        )
        monkeypatch.setattr(
            "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
            lambda *_a, **_k: (0, "HTTP/1.1 302 Found\nLocation: https://auth.example.com/\n", ""),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)}
        assert checks["healthz"].passed
