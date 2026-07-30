from __future__ import annotations

import json
from pathlib import Path

import httpx
from toolkit.controller.ssh_transport import SSHControllerTransport
from toolkit.core.config.config import Config


def _config() -> Config:
    return Config()


def test_ssh_controller_transport_keeps_token_remote(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def ssh_run(_cfg, address, command, **kwargs):
        captured.update(address=address, command=command, stdin=kwargs["stdin"])
        return 0, json.dumps({"status": "ok"}) + "\n__HOMELAB_CONTROLLER_STATUS__:200", ""

    monkeypatch.setattr("toolkit.controller.ssh_transport.ssh_run_on_vm", ssh_run)
    transport = SSHControllerTransport(_config(), tmp_path)
    request = httpx.Request("POST", "http://controller/v1/test", json={"value": "safe"})

    response = transport.handle_request(request)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert captured["address"] == "10.10.10.10"
    assert captured["stdin"] == '{"value":"safe"}'
    assert "local.token" in str(captured["command"])
    assert '"X-Controller-Token: $token"' in str(captured["command"])
    assert "unit-test-token" not in str(captured["command"])


def test_ssh_controller_transport_rejects_other_hosts(tmp_path: Path) -> None:
    transport = SSHControllerTransport(_config(), tmp_path)

    try:
        transport.handle_request(httpx.Request("GET", "http://example.test/v1/health"))
    except httpx.ConnectError as exc:
        assert "non-controller" in str(exc)
    else:
        raise AssertionError("non-controller request was accepted")
