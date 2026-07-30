"""Unit tests for uptime-kuma plugin verify()."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("uptime-kuma")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no uptime-kuma plugin")


class TestUptimeKumaVerify:
    def test_zero_monitors_fails_verification(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(notifications=True))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (0, "0"))
        monkeypatch.setattr(
            "importlib.import_module",
            lambda *_a, **_k: type("M", (), {"bootstrap_uptime_kuma": lambda *_x, **_y: []})(),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert not checks["monitors"].passed
        assert "no monitors" in checks["monitors"].detail

    def test_unreadable_monitor_database_fails_verification(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(notifications=True))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (1, "FAIL"))

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert not checks["monitors"].passed
        assert "probe failed" in checks["monitors"].detail

    def test_http_and_monitors(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(notifications=True))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (0, "5"))

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["status-page-http"].passed
        assert checks["monitors"].passed
        assert "5" in checks["monitors"].detail


def test_compose_selects_sqlite_for_unattended_first_boot():
    compose = yaml.safe_load(Path("toolkit/services/uptime-kuma/compose.yaml").read_text())

    environment = compose["services"]["uptime-kuma"]["environment"]
    assert environment["UPTIME_KUMA_DB_TYPE"] == "sqlite"


def test_bootstrap_completes_database_setup_phase(monkeypatch):
    bootstrap = importlib.import_module("toolkit.services.uptime-kuma.bootstrap")
    entry_pages = iter([{"type": "setup-database"}, {"type": "setup"}])
    posts: list[dict] = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        bootstrap.httpx,
        "get",
        lambda *_a, **_k: Response(next(entry_pages)),
    )
    monkeypatch.setattr(
        bootstrap.httpx,
        "post",
        lambda *_a, **kwargs: posts.append(kwargs["json"]) or Response({"ok": True}),
    )
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    assert bootstrap._ensure_database_ready("http://uptime-kuma:3001", timeout=2)
    assert posts == [{"dbConfig": {"type": "sqlite"}}]


def test_project_uses_maintained_uptime_kuma_v2_client():
    project = Path("pyproject.toml").read_text()

    assert "uptime-kuma-api2>=2.1.0" in project
    assert '"uptime-kuma-api>=' not in project


def test_bootstrap_requires_managed_admin_password(monkeypatch):
    bootstrap = importlib.import_module("toolkit.services.uptime-kuma.bootstrap")
    monkeypatch.setattr(bootstrap, "_wait_for_uptime_kuma", lambda *_a, **_k: True)
    monkeypatch.setattr(bootstrap, "_ensure_database_ready", lambda *_a, **_k: True)

    logs = bootstrap.bootstrap_uptime_kuma(Config(domain="example.com"), {})

    assert logs == ["Uptime Kuma: SSO_USER_PASSWORD is missing"]


def test_bootstrap_logs_in_after_setup_and_reconciles_monitors(monkeypatch):
    bootstrap = importlib.import_module("toolkit.services.uptime-kuma.bootstrap")
    calls: list[tuple] = []

    class FakeApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def need_setup(self):
            return True

        def setup(self, username, password):
            calls.append(("setup", username, password))

        def login(self, username, password):
            calls.append(("login", username, password))

        def get_monitors(self):
            return [{"id": 7, "name": "portal", "url": "https://old.example.com"}]

        def edit_monitor(self, monitor_id, **kwargs):
            calls.append(("edit", monitor_id, kwargs["name"], kwargs["url"]))

        def add_monitor(self, **kwargs):
            calls.append(("add", kwargs["name"], kwargs["url"]))

        def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(bootstrap, "_wait_for_uptime_kuma", lambda *_a, **_k: True)
    monkeypatch.setattr(bootstrap, "_ensure_database_ready", lambda *_a, **_k: True)
    monkeypatch.setitem(
        sys.modules,
        "uptime_kuma_api",
        SimpleNamespace(MonitorType=SimpleNamespace(HTTP="http"), UptimeKumaApi=FakeApi),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.dns.desired_records_from_config",
        lambda *_a, **_k: [
            SimpleNamespace(type="A", name="portal.example.com"),
            SimpleNamespace(type="A", name="new.example.com"),
        ],
    )

    logs = bootstrap.bootstrap_uptime_kuma(
        Config(domain="example.com"),
        {"SSO_USER_PASSWORD": "managed-password"},
    )

    assert calls[:2] == [
        ("setup", "admin", "managed-password"),
        ("login", "admin", "managed-password"),
    ]
    assert ("edit", 7, "portal", "https://portal.example.com") in calls
    assert ("add", "new", "https://new.example.com") in calls
    assert calls[-1] == ("disconnect",)
    assert "Uptime Kuma: reconciled 2 HTTP monitor(s)" in logs


def test_bootstrap_retries_transient_api_failure(monkeypatch):
    bootstrap = importlib.import_module("toolkit.services.uptime-kuma.bootstrap")
    attempts = 0

    class FakeApi:
        def __init__(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError

        def need_setup(self):
            return False

        def login(self, *_args):
            return None

        def get_monitors(self):
            return []

        def disconnect(self):
            return None

    monkeypatch.setattr(bootstrap, "_wait_for_uptime_kuma", lambda *_a, **_k: True)
    monkeypatch.setattr(bootstrap, "_ensure_database_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    monkeypatch.setitem(
        sys.modules,
        "uptime_kuma_api",
        SimpleNamespace(MonitorType=SimpleNamespace(HTTP="http"), UptimeKumaApi=FakeApi),
    )
    monkeypatch.setattr("toolkit.core.ops.dns.desired_records_from_config", lambda *_a, **_k: [])

    logs = bootstrap.bootstrap_uptime_kuma(
        Config(domain="example.com"),
        {"SSO_USER_PASSWORD": "managed-password"},
    )

    assert attempts == 2
    assert logs[-1] == "Uptime Kuma: reconciled 0 HTTP monitor(s)"
