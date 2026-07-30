from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.services.headscale.mesh import _resolve_infra_headscale_login


def test_router_uses_private_tls_ingress_instead_of_raw_loopback(monkeypatch) -> None:
    cfg = Config(domain="example.com")
    calls: list[list[str]] = []

    monkeypatch.setattr("toolkit.core.manifest.catalog.provider_service_name", lambda _capability: "caddy")
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda _cfg, _service: "10.10.10.10")

    def run(command: list[str], _timeout: int):
        calls.append(command)
        if command[0] == "sh":
            return 0, "", ""
        return 0, "200", ""

    logs: list[str] = []
    login = _resolve_infra_headscale_login(cfg, run, logs)

    assert login == "https://vpn.example.com"
    assert logs == []
    assert calls[0][0:2] == ["sh", "-c"]
    assert calls[0][3] == "homelab-headscale-hosts"
    assert calls[0][-3:] == ["homelab-headscale-private-ingress", "10.10.10.10", "vpn.example.com"]
    assert calls[1][-1] == "https://vpn.example.com/health"
    assert all("127.0.0.1:4080" not in part for command in calls for part in command)
