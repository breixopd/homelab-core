"""Functional verifier contract tests for FlareSolverr."""

from __future__ import annotations

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config


def _plugin():
    module = load_plugin("flaresolverr")
    plugin_type = next(
        value for name in dir(module) if name.endswith("Plugin") and isinstance((value := getattr(module, name)), type)
    )
    return plugin_type()


def test_solve_probe_uses_neutral_browser_target(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_exec(_cfg, _service, command, _vm_ip, _root, **_kwargs):
        commands.append(command)
        if "/health" in command[-1]:
            return 0, '{"status":"ok"}'
        return 0, '{"status":"ok","solution":{"status":200}}'

    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

    checks = _plugin().verify(Config(domain="example.test"), {}, "10.10.10.11", tmp_path)

    assert all(check.passed for check in checks)
    solve_command = commands[1][-1]
    assert "https://example.com" in solve_command
    assert "google.com" not in solve_command
