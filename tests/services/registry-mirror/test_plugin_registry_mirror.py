"""Unit tests for registry-mirror plugin verify()."""

from __future__ import annotations

import importlib
from unittest.mock import patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("registry-mirror")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no registry-mirror plugin")


def test_post_start_reconciles_host_registry_configuration(tmp_path):
    bootstrap = importlib.import_module("toolkit.services.registry-mirror.bootstrap")
    with patch.object(bootstrap, "ensure_registry_mirror", return_value=["Registry mirror: configured"]) as ensure:
        logs = _plugin().post_start(Config(), {}, root=tmp_path)

    assert logs == ["Registry mirror: configured"]
    ensure.assert_called_once_with(tmp_path)


class TestRegistryMirrorVerify:
    def test_skips_when_container_is_missing(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com")
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed is False
        assert checks[0].detail == "container missing"

    def test_ca_v2_pull_through(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))

        def fake_ssh(_cfg, _ip, cmd, root=None, timeout=30):
            if "ca.crt" in cmd or "/dev/null && echo OK" in cmd:
                return 0, "OK", ""
            if "registry-1.docker.io" in cmd:
                return 0, "401", ""
            return 1, "", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_health_status_on_vm", lambda *_a, **_k: ("running", "healthy"))
        monkeypatch.setattr("toolkit.services.sdk.registry_mirror_running", lambda **_: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "{}"))
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, "401"),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["running"].passed
        assert checks["ca_cert"].passed
        assert checks["v2_endpoint"].passed
        assert checks["pull_through"].passed
