"""Secret-handling regression tests for the Navidrome plugin."""

from __future__ import annotations

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def test_library_probe_does_not_put_password_in_command(monkeypatch, tmp_path):
    module = load_plugin("navidrome")
    plugin = module.NavidromePlugin()
    password = "navidrome-password-command-canary"
    calls = []

    def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        command = args[2]
        if command[:2] == ["sh", "-c"] and "find /music" in command[2]:
            return 0, "0"
        if command[:2] == ["sh", "-c"] and "test -d /music" in command[2]:
            return 0, "files"
        return 0, '{"subsonic-response":{"indexes":{"index":[]}}}'

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    plugin._check_library(
        Config(domain="example.com", services=ServicesConfig(media=True)),
        {"SSO_USER_PASSWORD": password},
        "10.10.10.12",
        tmp_path,
        lambda _secrets, _name: password,
    )
    command = calls[-1][0][2]
    assert password not in repr(command)
    assert calls[-1][1]["secret_environment"] == {"HOMELAB_VERIFY_PASSWORD": password}
