"""Unit tests for roundcube plugin verify()."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    for name in dir(mod := load_plugin("roundcube")):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError("no plugin")


class TestRoundcubeVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(email=True))
        checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)
        assert len(checks) == 1
        assert checks[0].passed
        assert "localhost" in checks[0].detail

    def test_login_and_imap(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(email=True))

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if "installer" in url:
                return 404, ""
            return 0, "<html>roundcube login username password</html>"

        def fake_exec(_cfg, container, cmd, _ip, _root, **kw):
            if "/dev/tcp/mailserver/143" in " ".join(cmd):
                return 0, "IMAP_OK"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)}
        assert checks["login_page"].passed
        assert checks["installer_disabled"].passed
        assert checks["imap_backend"].passed


def test_roundcube_apache_can_drop_privileges_and_health_rejects_internal_error() -> None:
    compose = yaml.safe_load(Path("toolkit/services/roundcube/compose.yaml").read_text())
    service = compose["services"]["roundcube"]

    assert service["cap_drop"] == ["ALL"]
    assert service["cap_add"] == ["SETGID", "SETUID"]
    healthcheck = " ".join(service["healthcheck"]["test"])
    assert "roundcube" in healthcheck.lower()
    assert "[ $$? -ne 7 ]" not in healthcheck


def test_forward_auth_contract_is_declared_by_compiled_manifest():
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))

    check = _plugin()._check_forward_auth_contract(cfg)

    assert check.passed
    assert check.check == "forward_auth_contract"
    assert "forward_auth" in check.detail


def test_missing_enabled_container_fails(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_args, **_kwargs: False)

    checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)

    assert len(checks) == 1
    assert checks[0].check == "container"
    assert not checks[0].passed


def test_forward_auth_contract_fails_when_manifest_route_uses_native_auth(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    route = SimpleNamespace(
        service="roundcube",
        match=None,
        auth=SimpleNamespace(mode="native"),
        host="mail.example.com",
        upstream="roundcube:80",
    )
    monkeypatch.setattr("toolkit.core.manifest.routes.compile_routes", lambda _cfg: (route,))

    check = _plugin()._check_forward_auth_contract(cfg)

    assert not check.passed
    assert "native" in check.detail
