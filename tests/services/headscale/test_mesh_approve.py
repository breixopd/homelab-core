from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.services.headscale.bootstrap import approve_mesh_registration, personal_headscale_username


def test_personal_headscale_username_from_email():
    cfg = Config(domain="example.com", email="alice@example.com")
    assert personal_headscale_username(cfg) == "alice"


def test_approve_mesh_registration_creates_user_and_registers(monkeypatch):
    cfg = Config(domain="example.com", email="brei@example.com")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if "users" in cmd and "list" in cmd:
            return 0, "[]", ""
        if "users" in cmd and "create" in cmd:
            return 0, "", ""
        if "nodes" in cmd and "register" in cmd:
            return 0, "registered", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap._run_cmd",
        lambda _cfg, _root: fake_run,
    )
    logs = approve_mesh_registration(cfg, Path("."), key="test-key-123")
    assert any("registered node" in line for line in logs)
    register_cmds = [c for c in calls if "nodes" in c and "register" in c]
    assert register_cmds
    assert "--user" in register_cmds[0] and "brei" in register_cmds[0]
