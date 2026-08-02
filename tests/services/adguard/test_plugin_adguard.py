"""Unit tests for adguard plugin verify()."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.verify.models import VerifyStatus


def _plugin():
    return load_plugin("adguard").AdguardPlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(management=True, cloud=True))


def test_post_start_bootstraps_and_syncs_all_rewrites(tmp_path, monkeypatch):
    client = MagicMock(
        sync_mesh_service_rewrites=MagicMock(return_value={"created": 1, "updated": 0, "unchanged": 2}),
        sync_internal_dns=MagicMock(return_value={"created": 3, "updated": 1}),
        sync_external_hosts_rewrites=MagicMock(return_value={"created": 2, "updated": 0}),
        sync_mesh_node_rewrites=MagicMock(return_value={"created": 1, "updated": 1, "removed": 0}),
    )
    monkeypatch.setattr("toolkit.services.adguard.bootstrap.bootstrap_adguard", lambda *_a, **_k: ["AdGuard: ready"])
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_a, **_k: "10.10.10.10")
    monkeypatch.setattr("toolkit.core.ops.dns.AdGuardDNS", lambda **_kwargs: client)
    monkeypatch.setattr("toolkit.services.headscale.bootstrap.list_mesh_nodes", lambda *_a, **_k: [{"name": "node"}])
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    with patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://adguard:3000"):
        logs = _plugin().post_start(_cfg(), {"ADGUARD_ADMIN_PASSWORD": "pw"}, root=tmp_path)

    assert logs[0] == "AdGuard: ready"
    assert any("mesh +1 ~0" in line and "external +2 ~0" in line for line in logs)


def test_post_start_retries_transient_connect_error(tmp_path, monkeypatch):
    import httpx

    failure = MagicMock(
        sync_mesh_service_rewrites=MagicMock(side_effect=httpx.ConnectError("warming up")),
    )
    success = MagicMock(
        sync_mesh_service_rewrites=MagicMock(return_value={"created": 0, "updated": 0, "unchanged": 0}),
        sync_internal_dns=MagicMock(return_value={"created": 0, "updated": 0}),
        sync_external_hosts_rewrites=MagicMock(return_value={"created": 0, "updated": 0}),
    )
    clients = iter([failure, success])
    monkeypatch.setattr("toolkit.services.adguard.bootstrap.bootstrap_adguard", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_a, **_k: "10.10.10.10")
    monkeypatch.setattr("toolkit.core.ops.dns.AdGuardDNS", lambda **_kwargs: next(clients))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    with patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://adguard:3000"):
        logs = _plugin().post_start(_cfg(), {"ADGUARD_ADMIN_PASSWORD": "pw"}, root=tmp_path)

    assert any("mesh +0 ~0" in line for line in logs)


class TestAdguardVerify:
    def test_disabled_public_dns_is_not_applicable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.core.ops.dns.dns_public_access_enabled", lambda _cfg: False)

        check = _plugin()._check_dns_public(_cfg(), "10.10.10.10", tmp_path)

        assert check.status is VerifyStatus.NOT_APPLICABLE

    def test_public_dns_missing_a_record_is_not_ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.core.ops.dns.dns_public_access_enabled", lambda _cfg: True)
        monkeypatch.setattr("toolkit.core.ops.dns.resolve_public_dns_ip", lambda _cfg: ("203.0.113.10", ""))
        monkeypatch.setattr("toolkit.core.ops.dns.dns_resolver_fqdn", lambda _cfg: "dns.example.com")
        monkeypatch.setattr(
            load_plugin("adguard"),
            "ssh_on_vm",
            lambda *_a, **_k: (0, "*:53\n", ""),
        )
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")):
            check = _plugin()._check_dns_public(_cfg(), "10.10.10.10", tmp_path)
        assert check.passed is False
        assert check.status is VerifyStatus.NOT_READY

    def test_public_dns_without_resolver_ip_is_not_ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.core.ops.dns.dns_public_access_enabled", lambda _cfg: True)
        monkeypatch.setattr("toolkit.core.ops.dns.resolve_public_dns_ip", lambda _cfg: ("", "not configured"))
        monkeypatch.setattr("toolkit.core.ops.dns.dns_resolver_fqdn", lambda _cfg: "dns.example.com")
        monkeypatch.setattr(
            load_plugin("adguard"),
            "ssh_on_vm",
            lambda *_a, **_k: (0, "*:53\n", ""),
        )

        check = _plugin()._check_dns_public(_cfg(), "10.10.10.10", tmp_path)

        assert check.passed is False
        assert check.status is VerifyStatus.NOT_READY

    def test_protection_status_and_dns_resolve(self, tmp_path, monkeypatch):
        rewrites = [
            {"domain": "auth.example.com", "answer": "10.10.10.10"},
            {"domain": "grafana.example.com", "answer": "10.10.10.10"},
        ]
        monkeypatch.setattr(load_plugin("adguard"), "adguard_list_rewrites", lambda *_a, **_k: (rewrites, ""))
        monkeypatch.setattr(
            "toolkit.core.manifest.routes.private_routes",
            lambda _cfg: (SimpleNamespace(host="grafana.example.com"),),
        )
        monkeypatch.setattr(
            "toolkit.core.ops.dns.external_hosts_private_rewrites",
            lambda _cfg: {},
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (
                0,
                json.dumps({"running": True, "protection_enabled": True, "protection_disabled_duration": 0}),
            ),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "10.10.10.10\n", ""),
        )

        checks = {
            c.check: c for c in _plugin().verify(_cfg(), {"ADGUARD_ADMIN_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["dns_rewrites"].passed
        assert checks["fqdn_set"].passed
        assert checks["protection_status"].passed
        assert checks["dns_resolve"].passed
        assert "auth.example.com" in checks["dns_resolve"].detail

    def test_protection_status_fails_when_paused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            load_plugin("adguard"),
            "adguard_list_rewrites",
            lambda *_a, **_k: ([{"domain": "x.example.com"}], ""),
        )
        monkeypatch.setattr("toolkit.core.manifest.routes.private_routes", lambda _cfg: ())
        monkeypatch.setattr("toolkit.core.ops.dns.external_hosts_private_rewrites", lambda _cfg: {})
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (
                0,
                json.dumps({"running": True, "protection_enabled": False, "protection_disabled_duration": 60}),
            ),
        )
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", lambda *_a, **_k: (0, "10.0.0.1", ""))

        checks = {
            c.check: c for c in _plugin().verify(_cfg(), {"ADGUARD_ADMIN_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["fqdn_set"].passed
        assert checks["protection_status"].passed is False
