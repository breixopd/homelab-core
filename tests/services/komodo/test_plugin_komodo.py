"""Unit tests for komodo-core and komodo-mongo plugin verify()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services.sdk import VerifyCheck


def _plugin(service: str):
    for name in dir(mod := load_plugin(service)):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError(f"no plugin for {service}")


def test_mongo_healthcheck_is_authenticated_and_process_bounded() -> None:
    compose = yaml.safe_load(
        (Path(__file__).parents[3] / "toolkit/services/komodo-mongo/compose.yaml").read_text(encoding="utf-8")
    )
    health = compose["services"]["komodo-mongo"]["healthcheck"]
    command = health["test"][1]

    assert "timeout 15s mongosh" in command
    assert "serverSelectionTimeoutMS=3000" in command
    assert "connectTimeoutMS=3000" in command
    assert '"$$MONGO_INITDB_ROOT_USERNAME"' in command
    assert '"$$MONGO_INITDB_ROOT_PASSWORD"' in command
    assert health["timeout"] == "20s"
    assert health["interval"] == "60s"


class TestKomodoCoreVerify:
    def test_api_and_oidc(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))

        def fake_exec(_cfg, _c, cmd, _ip, _root, **_kw):
            joined = " ".join(cmd)
            if "/dev/tcp/komodo-mongo" in joined:
                return 0, "MONGO_OK"
            return (
                0,
                "KOMODO_OIDC_ENABLED=true\n"
                "KOMODO_OIDC_PROVIDER=https://auth.example.com\n"
                "KOMODO_OIDC_CLIENT_ID=komodo\n"
                "KOMODO_OIDC_CLIENT_SECRET=secret",
            )

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        monkeypatch.setattr(
            "toolkit.services.sdk.oidc_check_auth_discovery_route",
            lambda *_a, **_k: VerifyCheck("komodo", "oidc_token_route", True, "ok"),
        )

        checks = {c.check: c for c in _plugin("komodo-core").verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["api_health"].passed
        assert checks["mongo_connect"].passed
        assert checks["oidc_enabled"].passed
        assert checks["oidc_issuer"].passed


class TestKomodoMongoVerify:
    def test_ping_and_status(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        commands: list[list[str]] = []
        call_kwargs: list[dict] = []
        ready_attempts = 0

        def fake_exec(_cfg, _c, cmd, _ip, _root, **kw):
            nonlocal ready_attempts
            commands.append(cmd)
            call_kwargs.append(kw)
            if "{hello:1}" in " ".join(cmd):
                ready_attempts += 1
                if ready_attempts == 1:
                    return 1, "not ready"
                return 0, "1"
            return 0, '{ "ok" : 1 }'

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        monkeypatch.setattr("time.sleep", lambda _seconds: None)

        checks = {
            c.check: c
            for c in _plugin("komodo-mongo").verify(cfg, {"KOMODO_DATABASE_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["ping"].passed
        assert checks["ready"].passed
        assert ready_attempts == 2
        ping_command = next(" ".join(command) for command in commands if "adminCommand('ping')" in " ".join(command))
        assert "pw" not in ping_command
        assert "$MONGO_INITDB_ROOT_PASSWORD" in ping_command
        assert "timeout 15s mongosh" in ping_command
        assert "serverSelectionTimeoutMS=3000" in ping_command
        assert "connectTimeoutMS=3000" in ping_command
        ready_command = next(" ".join(command) for command in commands if "{hello:1}" in " ".join(command))
        assert "pw" not in ready_command
        assert "$MONGO_INITDB_ROOT_PASSWORD" in ready_command
        assert "timeout 15s mongosh" in ready_command
        assert "serverSelectionTimeoutMS=3000" in ready_command
        assert "connectTimeoutMS=3000" in ready_command
        assert all("secret_environment" not in kwargs for kwargs in call_kwargs)


def _mongo_lifecycle_context(tmp_path: Path) -> MagicMock:
    context = MagicMock(root=tmp_path, node="infra")
    values = {
        "KOMODO_DATABASE_PASSWORD": "new-password",
        "KOMODO_DATABASE_USERNAME": "komodo",
        "KOMODO_MONGO_DATA_SOURCE": str(tmp_path / "mongo"),
    }
    context.environment.side_effect = lambda name, default="": values.get(name, default)
    context.run_host.return_value = MagicMock(returncode=0)
    return context


def test_komodo_mongo_rotation_accepts_desired_password_without_restart(tmp_path: Path, monkeypatch) -> None:
    context = _mongo_lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(_service, command, **kwargs):
        calls.append({"command": command, **kwargs})
        return 0, '{"ok": 1}'

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    services = _plugin("komodo-mongo").before_runtime_start(context, ("komodo-mongo",))

    assert services == ("komodo-mongo",)
    assert context.run_host.call_args_list == [context.run_host.call_args_list[0]]
    assert calls[0]["secret_environment"] == {"MONGO_USERNAME": "komodo", "MONGO_NEW_PASSWORD": "new-password"}
    assert "new-password" not in " ".join(calls[0]["command"])
    context.log.assert_called_once()


def test_komodo_mongo_rotation_uses_authenticated_runtime_and_secret_stdin(tmp_path: Path, monkeypatch) -> None:
    context = _mongo_lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(_service, command, **kwargs):
        calls.append({"service": _service, "command": command, **kwargs})
        return (1, "auth failed") if len(calls) == 1 else (0, "ok")

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    _plugin("komodo-mongo").before_runtime_start(context, ("komodo-mongo",))

    assert context.run_host.call_args_list[1].args[0] == ["docker", "stop", "komodo-core"]
    assert calls[1]["secret_environment"] == {"MONGO_USERNAME": "komodo", "MONGO_NEW_PASSWORD": "new-password"}
    assert "new-password" not in " ".join(calls[1]["command"])
    assert not any("komodo-mongo-password-recovery" in str(call) for call in context.run_host.call_args_list)


def test_komodo_mongo_rotation_falls_back_to_network_isolated_recovery(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "mongo"
    source.mkdir()
    context = _mongo_lifecycle_context(tmp_path)
    calls: list[dict] = []

    def fake_exec(service, command, **kwargs):
        calls.append({"service": service, "command": command, **kwargs})
        # Desired auth and authenticated change fail; isolated recovery works.
        return (1, "auth failed") if service == "komodo-mongo" else (0, "ok")

    monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    _plugin("komodo-mongo").before_runtime_start(context, ("komodo-mongo",))

    run_args = next(
        call.args[0] for call in context.run_host.call_args_list if call.args[0][:3] == ["docker", "run", "-d"]
    )
    assert "--network" in run_args and run_args[run_args.index("--network") + 1] == "none"
    assert "--mount" in run_args and str(source) in run_args[run_args.index("--mount") + 1]
    assert ["docker", "rm", "-f", "komodo-mongo-password-recovery"] in [
        call.args[0] for call in context.run_host.call_args_list
    ]
    assert all("new-password" not in str(call) for call in context.run_host.call_args_list)
    assert calls[-1]["secret_environment"] == {"MONGO_USERNAME": "komodo", "MONGO_NEW_PASSWORD": "new-password"}
