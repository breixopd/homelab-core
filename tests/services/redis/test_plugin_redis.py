"""Unit tests for redis and dev-redis plugin verify()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin(service: str):
    module = load_plugin(service)
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError(f"no plugin class in {service}")


class TestRedisVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        checks = _plugin("redis").verify(cfg, {"REDIS_PASSWORD": "x"}, "10.10.10.10", tmp_path)
        assert checks[0].passed
        assert "localhost" in checks[0].detail

    def test_ping_memory_acl(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_exec(_cfg, container, cmd, _ip, _root, timeout=15, user="", **kwargs):
            calls.append((cmd, kwargs.get("secret_environment")))
            joined = " ".join(cmd)
            if "ping" in joined.lower():
                return 0, "PONG"
            if "INFO memory" in joined:
                return 0, "maxmemory:1048576\nused_memory:1024\nmaxmemory_policy:allkeys-lru"
            if "ACL LIST" in joined:
                return 0, "user default on sanitize-payload #password"
            if "INFO keyspace" in joined:
                return 0, "db0:keys=3,expires=1"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {
            c.check: c for c in _plugin("redis").verify(cfg, {"REDIS_PASSWORD": "secret"}, "10.10.10.10", tmp_path)
        }
        assert checks["ping"].passed
        assert checks["memory"].passed
        assert checks["acl_auth"].passed
        assert calls
        assert all("secret" not in " ".join(cmd) for cmd, _ in calls)
        assert all(env == {"REDISCLI_AUTH": "secret"} for _, env in calls)


class TestDevRedisVerify:
    def test_skips_missing_container(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin("dev-redis").verify(cfg, {}, "10.10.10.12", tmp_path)
        assert checks[0].passed

    def test_ping_and_memory(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
        password = "dev-redis-command-canary"
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_exec(_cfg, container, cmd, _ip, _root, timeout=15, user="", **kwargs):
            calls.append((cmd, kwargs.get("secret_environment")))
            joined = " ".join(cmd)
            if "ping" in joined.lower():
                return 0, "PONG"
            if "INFO memory" in joined:
                return 0, "maxmemory:0\nused_memory:100"
            if "ACL LIST" in joined:
                return 0, "user default on"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {
            c.check: c
            for c in _plugin("dev-redis").verify(cfg, {"DEV_REDIS_PASSWORD": password}, "10.10.10.12", tmp_path)
        }
        assert checks["ping"].passed
        assert checks["memory"].passed
        assert calls
        assert all(password not in " ".join(cmd) for cmd, _ in calls)
        assert all(env == {"REDISCLI_AUTH": password} for _, env in calls)


def _lifecycle_context(tmp_path: Path) -> MagicMock:
    context = MagicMock(root=tmp_path, node="infra")
    context.environment.side_effect = lambda name, default="": {"REDIS_PASSWORD": "new-password"}.get(name, default)
    context.run_host.return_value = MagicMock(returncode=0)
    context.compose.return_value = MagicMock(returncode=0)
    context.wait_until_healthy.return_value = True
    return context


def test_rotation_noop_when_desired_password_is_accepted(tmp_path: Path, monkeypatch) -> None:
    context = _lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(service, command, **kwargs):
        calls.append({"service": service, "command": command, **kwargs})
        return 0, "PONG"

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    assert _plugin("redis").before_runtime_start(context, ("redis",)) == ("redis",)
    assert len(calls) == 1
    assert calls[0]["secret_environment"] == {"REDISCLI_AUTH": "new-password"}
    assert "new-password" not in " ".join(calls[0]["command"])
    context.compose.assert_not_called()


def test_rotation_reconciles_in_place_from_mounted_config(tmp_path: Path, monkeypatch) -> None:
    context = _lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(service, command, **kwargs):
        calls.append({"service": service, "command": command, **kwargs})
        if len(calls) == 1:
            # redis-cli returns zero even for this protocol-level failure.
            return 0, "AUTH failed: WRONGPASS\nNOAUTH Authentication required"
        return (0, "OK") if "CONFIG SET" in " ".join(command) else (0, "PONG")

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    assert _plugin("redis").before_runtime_start(context, ("redis",)) == ("redis",)
    migration = calls[1]
    assert migration["secret_environment"] == {"REDIS_NEW_PASSWORD": "new-password"}
    assert "-x CONFIG SET requirepass" in " ".join(migration["command"])
    assert "old-password" not in " ".join(migration["command"])
    assert "new-password" not in " ".join(migration["command"])
    context.compose.assert_not_called()


def test_rotation_force_recreates_only_redis_and_fails_closed(tmp_path: Path, monkeypatch) -> None:
    context = _lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(service, command, **kwargs):
        calls.append({"service": service, "command": command, **kwargs})
        return 1, "auth failed"

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    try:
        _plugin("redis").before_runtime_start(context, ("redis",))
    except RuntimeError as exc:
        assert "after force-recreate" in str(exc)
    else:
        raise AssertionError("expected reconciliation failure")
    context.compose.assert_called_once_with("up", "-d", "--force-recreate", "--no-deps", "redis")
    assert all(
        secret not in str(call)
        for call in context.run_host.call_args_list
        for secret in ("old-password", "new-password")
    )


def test_rotation_skips_absent_container_without_exec(tmp_path: Path, monkeypatch) -> None:
    context = _lifecycle_context(tmp_path)
    context.run_host.return_value = MagicMock(returncode=1)
    called = False

    def fake_exec(*_args, **_kwargs):
        nonlocal called
        called = True
        return 1, ""

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    assert _plugin("redis").before_runtime_start(context, ("redis",)) == ("redis",)
    assert not called
