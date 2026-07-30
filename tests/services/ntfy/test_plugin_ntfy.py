"""Unit tests for ntfy plugin verify()."""

from __future__ import annotations

import json

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("ntfy")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no ntfy plugin")


class TestNtfyVerify:
    def test_roundtrip(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(notifications=True))
        marker_holder: list[str] = []

        def fake_exec(_cfg, container, cmd, _ip, _root, timeout=15, user=""):
            joined = " ".join(cmd)
            if "wget" in joined and "post-data" in joined:
                for part in joined.split("post-data="):
                    if part.startswith('"verify-'):
                        marker_holder.append(part.strip('"').split()[0])
                return 0, ""
            return 1, ""

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if "/v1/health" in url:
                return 0, "ok"
            if "/json?poll" in url:
                msg = marker_holder[0] if marker_holder else "verify-1"
                # ntfy returns NDJSON (one object per line), not a JSON array
                return 0, json.dumps({"message": "older"}) + "\n" + json.dumps({"message": msg})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["publish"].passed
        assert checks["roundtrip"].passed


def test_ntfy_post_start_initializes_owned_topics(monkeypatch):
    client = type("Client", (), {"send": lambda self, *_args, **_kwargs: True})()
    monkeypatch.setattr("toolkit.services.ntfy.client.NtfyClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("toolkit.services.ntfy.client.resolve_local_ntfy_base", lambda: "http://ntfy")

    logs = _plugin().post_start(Config(), {}, root=None)

    assert len(logs) == 5
    assert all("Initialized ntfy topic" in line for line in logs)
