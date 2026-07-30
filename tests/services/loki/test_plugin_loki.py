"""Unit tests for loki plugin verify()."""

from __future__ import annotations

import json

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin(service: str | None = None):
    service = service or "loki"
    module = load_plugin(service)
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError(f"no plugin class in {service}")


class TestLokiVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        checks = _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed

    def test_ready_labels_ingest(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        streams = {"data": {"result": [{"stream": {"job": "docker"}, "values": [["1", "line"]]}]}}

        def fake_ssh(_cfg, _ip, cmd, **_kw):
            if "/ready" in cmd:
                return 0, "ready", ""
            if "/labels" in cmd:
                return 0, json.dumps({"data": ["job", "host"]}), ""
            if "query_range" in cmd:
                return 0, json.dumps(streams), ""
            return 1, "", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["ready"].passed
        assert checks["labels"].passed
        assert checks["log_ingest"].passed

    def test_ingest_fails_without_streams(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))

        def fake_ssh(_cfg, _ip, cmd, **_kw):
            if "/ready" in cmd:
                return 0, "ready", ""
            if "/labels" in cmd:
                return 0, json.dumps({"data": ["job"]}), ""
            if "query_range" in cmd:
                return 0, json.dumps({"data": {"result": []}}), ""
            return 1, "", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["log_ingest"].passed is False
