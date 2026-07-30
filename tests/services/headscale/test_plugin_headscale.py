"""Unit tests for headscale plugin verify()."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ExternalHost, ServicesConfig
from toolkit.core.verify.models import VerifyStatus


def _plugin():
    return load_plugin("headscale").HeadscalePlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(security=True, management=True, cloud=True))


_HEADSCALE_CFG = {
    "oidc": {
        "issuer": "https://auth.example.com",
        "scope": ["openid", "profile", "email", "groups"],
        "pkce": {"enabled": True, "method": "S256"},
    }
}


def test_post_start_reconciles_health_oidc_preauth_and_router(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.ensure_headscale_oidc_provider",
        lambda *_a, **_k: ["Headscale: OIDC ready"],
    )
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.bootstrap_headscale_preauth",
        lambda **_k: ["Headscale: preauth key ready"],
    )
    monkeypatch.setattr(
        "toolkit.services.headscale.mesh.bootstrap_infra_subnet_router",
        lambda *_a, **_k: ["Headscale: subnet router ready"],
    )
    with (
        patch("toolkit.services.sdk.wait_for_http", return_value=True),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://headscale:8080"),
    ):
        logs = _plugin().post_start(_cfg(), {}, root=tmp_path)

    assert logs == [
        "Headscale: API reachable",
        "Headscale: OIDC ready",
        "Headscale: preauth key ready",
        "Headscale: subnet router ready",
    ]


def test_post_start_fails_when_preauth_key_cannot_be_created(tmp_path, monkeypatch):
    monkeypatch.setattr("toolkit.services.headscale.bootstrap.ensure_headscale_oidc_provider", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.bootstrap_headscale_preauth",
        lambda **_k: ["Headscale: preauth key create failed"],
    )
    with (
        patch("toolkit.services.sdk.wait_for_http", return_value=True),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://headscale:8080"),
        pytest.raises(RuntimeError, match="preauth"),
    ):
        _plugin().post_start(_cfg(), {}, root=tmp_path)


def test_host_cleanup_revokes_only_exact_matching_mesh_nodes(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_exec(_cfg, _container, command, _ip, _root, **_kwargs):
        calls.append(command)
        if command[:3] == ["headscale", "nodes", "list"]:
            return 0, json.dumps(
                [
                    {"id": 7, "name": "edge-01", "given_name": "edge-01"},
                    {"id": 8, "name": "edge-010", "given_name": "edge-010"},
                ]
            )
        return 0, ""

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    host = ExternalHost(name="edge-01", ip="192.0.2.20", services=["vpn-client"])

    logs = _plugin().cleanup_host_integration("vpn-client", _cfg(), host, tmp_path)

    assert calls[-1] == ["headscale", "nodes", "delete", "--identifier", "7", "--force"]
    assert all("8" not in command for command in calls[1:])
    assert logs == ["Headscale: revoked 1 mesh node(s) for edge-01"]


def test_deselecting_mesh_integration_runs_revocation(tmp_path, monkeypatch):
    plugin = _plugin()
    cleanup = MagicMock(return_value=["revoked"])
    monkeypatch.setattr(plugin, "cleanup_host_integration", cleanup)
    host = ExternalHost(name="edge-01", ip="192.0.2.20")

    assert plugin.reconcile_host_integration("vpn-client", _cfg(), host, tmp_path, selected=False) == ["revoked"]
    cleanup.assert_called_once_with("vpn-client", _cfg(), host, tmp_path)


def test_optional_subnet_router_and_acl_checks_are_not_applicable(tmp_path, monkeypatch):
    module = load_plugin("headscale")
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), "is_multi_node", property(lambda _self: False))
    cfg.services = ServicesConfig(security=False)

    router = module.check_subnet_router(cfg, "10.10.10.10", tmp_path)
    acl = module.check_acl(cfg, tmp_path)

    assert router.status is VerifyStatus.NOT_APPLICABLE
    assert acl.status is VerifyStatus.NOT_APPLICABLE


def test_management_status_and_resources_expose_mesh_inventory(tmp_path, monkeypatch):
    nodes = [
        {
            "name": "edge-01",
            "given_name": "edge-01",
            "ip_addresses": ["100.64.0.4", "fd7a:115c:a1e0::4"],
            "online": True,
            "last_seen": {"seconds": 1_700_000_000},
            "user": {"display_name": "Tagged Devices"},
        },
        {
            "name": "phone",
            "ip_addresses": ["100.64.0.5"],
            "online": False,
            "user": {"name": "brei"},
        },
    ]

    def fake_exec(_cfg, _container, command, _ip, _root, **_kwargs):
        return (0, json.dumps([{"name": "brei"}])) if "users" in command else (0, json.dumps(nodes))

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

    status = _plugin().status(_cfg(), {}, tmp_path)
    resources = _plugin().resources(_cfg(), {}, tmp_path)

    assert status == {"registered_nodes": 2, "online_nodes": 1, "users": 1}
    assert resources["mesh_nodes"][0] == {
        "name": "edge-01",
        "user": "Tagged Devices",
        "addresses": "100.64.0.4, fd7a:115c:a1e0::4",
        "state": "Online",
        "last_seen": "2023-11-14 22:13 UTC",
    }
    assert resources["mesh_nodes"][1]["state"] == "Offline"
    assert resources["mesh_nodes"][1]["last_seen"] == "Never"


class TestHeadscaleVerify:
    def test_api_health_and_users(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda _cfg, container, cmd, _ip, _root, **_kw: {
                ("cat",): (0, yaml.dump(_HEADSCALE_CFG)),
                ("headscale",): (
                    (0, json.dumps({"status": "pass"}))
                    if "health" in str(cmd)
                    else (0, json.dumps([{"id": "1", "name": "owner"}]))
                ),
            }.get((cmd[1],) if len(cmd) > 1 and cmd[0] == "/bin/busybox" else (cmd[0],), (0, "")),
        )

        def smarter_exec(_cfg, container, cmd, _ip, _root, **_kw):
            joined = " ".join(cmd)
            if "cat" in joined and "config.yaml" in joined:
                return 0, yaml.dump(_HEADSCALE_CFG)
            if "/health" in joined:
                return 0, json.dumps({"status": "pass"})
            if "users" in cmd:
                return 0, json.dumps([{"name": "owner"}])
            if "nodes" in cmd:
                return 0, json.dumps([{"online": True}])
            return 0, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", smarter_exec)
        monkeypatch.setattr(
            "toolkit.services.sdk.oidc_check_auth_discovery_route",
            lambda *_a, **_k: MagicMock(passed=True, check="oidc_token_route", detail="ok"),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "INF OIDC provider configured", ""),
        )
        monkeypatch.setattr(
            load_plugin("headscale"),
            "check_subnet_router",
            lambda *_a, **_k: MagicMock(passed=True, check="subnet_router", detail="ok"),
        )
        acl = tmp_path / "generated" / "headscale"
        acl.mkdir(parents=True)
        (acl / "acl.hujson").write_text('{"tagOwners":{},"autoApprovers":{},"autogroup:member":true}')

        checks = {c.check: c for c in _plugin().verify(_cfg(), {}, "10.10.10.10", tmp_path)}
        assert checks["api_health"].passed
        assert checks["users"].passed
        assert checks["oidc_issuer"].passed

    def test_check_users_skips_localhost(self, tmp_path):
        from toolkit.services.headscale.plugin import check_users

        check = check_users(_cfg().__class__(domain="localhost"), "10.10.10.10", tmp_path)
        assert check.passed and "localhost" in check.detail
