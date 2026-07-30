"""Unit tests for portal and homelab-ui plugin verify()."""

from __future__ import annotations

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin(service: str):
    for name in dir(mod := load_plugin(service)):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError(f"no plugin for {service}")


class TestPortalVerify:
    def test_localhost_skip(self, tmp_path, monkeypatch):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin("portal").verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed
        assert "localhost" in checks[0].detail


class TestHomelabUiVerify:
    def test_skips_missing_container(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin("homelab-ui").verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed is False
        assert checks[0].detail == "container missing"

    def test_ui_reachable(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "<html>"))
        checks = _plugin("homelab-ui").verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed
