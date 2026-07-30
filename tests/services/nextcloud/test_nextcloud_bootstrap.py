from __future__ import annotations

from toolkit.services.nextcloud import bootstrap


def test_nextcloud_bootstrap_commands_use_bounded_docker_timeout(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_exec(service, command, **kwargs):
        calls.append({"service": service, "command": command, **kwargs})
        return 0, "ok"

    monkeypatch.setattr(bootstrap, "docker_exec", fake_exec)

    assert bootstrap._nextcloud_exec(["php", "occ", "status"]) == (0, "ok")
    assert calls == [
        {
            "service": "nextcloud",
            "command": ["php", "occ", "status"],
            "user": "www-data",
            "timeout": 20,
        }
    ]
