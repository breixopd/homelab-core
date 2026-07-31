from pathlib import Path
from types import SimpleNamespace

from tests.helpers.plugins import load_plugin


def test_solve_timeout_is_not_success(monkeypatch, tmp_path: Path):
    module = load_plugin("flaresolverr")
    plugin = next(getattr(module, name)() for name in dir(module) if name.endswith("Plugin"))
    cfg = SimpleNamespace(domain="example.com", is_multi_node=True)
    calls = []

    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

    def exec_on_vm(_cfg, _service, command, *_args, **_kwargs):
        calls.append(command)
        return (0, '{"status":"ok"}') if len(calls) == 1 else (1, "timeout")

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", exec_on_vm)
    checks = {check.check: check for check in plugin.verify(cfg, {}, "10.0.0.2", tmp_path)}

    assert checks["health"].passed
    assert not checks["solve"].passed
    assert checks["solve"].status.value == "fail"
