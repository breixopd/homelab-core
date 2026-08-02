from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

from tests.helpers.machines import machines_with_addresses
from toolkit.core.config.config import Config, DNSConfig, NetworkConfig, ServicesConfig
from toolkit.core.manifest.routes import compile_routes
from toolkit.core.ops.hook_verify import (
    _check_caddy_forward_auth_route,
    _check_caddy_forward_auth_routes_batch,
    _check_caddy_split_native_paths,
    _check_cloudflare_public_dns_parity,
    _check_forward_auth_routes,
    _check_private_fqdns_not_in_cloudflare,
)
from toolkit.core.verify.models import VerifyCheck

HEADSCALE_MESH = importlib.import_module("toolkit.services.headscale.mesh")


def test_split_native_path_check_rejects_authelia_redirect(monkeypatch):
    cfg = Config(domain="example.com")
    output = (
        "__HOMELAB_SPLIT_0__\t401\t\n"
        "__HOMELAB_SPLIT_1__\t302\thttps://auth.example.com/?rd=https://fmd.example.com/api/v1/key\n"
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (0, output, ""),
    )

    checks = _check_caddy_split_native_paths(
        cfg,
        "fmd-server",
        "fmd.example.com",
        ("/version", "/api/v1/key"),
        "10.10.10.10",
        Path("."),
    )

    assert checks[0].passed
    assert not checks[1].passed
    assert "auth.example.com" in checks[1].detail


def test_split_native_path_accepts_only_documented_server_error(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (0, "__HOMELAB_SPLIT_0__\t500\t\n", ""),
    )

    checks = _check_caddy_split_native_paths(
        cfg,
        "romm",
        "romm.example.com",
        ("/api/oauth/openid",),
        "10.10.10.10",
        Path("."),
        probe_statuses=(500,),
    )

    assert checks[0].passed


def test_split_native_path_rejects_undocumented_gateway_error(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (0, "__HOMELAB_SPLIT_0__\t503\t\n", ""),
    )

    checks = _check_caddy_split_native_paths(
        cfg,
        "romm",
        "romm.example.com",
        ("/api/oauth/openid",),
        "10.10.10.10",
        Path("."),
        probe_statuses=(500,),
    )

    assert not checks[0].passed


def test_forward_auth_probe_originates_from_private_peer(monkeypatch, tmp_path):
    cfg = Config(domain="example.com")
    observed: dict[str, str] = {}

    def fake_ssh(_cfg, source_ip, command, **_kwargs):
        observed["source_ip"] = source_ip
        observed["command"] = command
        return 0, "HTTP/2 302\r\nlocation: https://auth.example.com/\r\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    check = _check_caddy_forward_auth_route(
        cfg,
        "prometheus",
        "prometheus.example.com",
        cfg.node_ip("media"),
        cfg.node_ip("infra"),
        tmp_path,
    )

    assert check.passed
    assert observed["source_ip"] == cfg.node_ip("media")
    assert f"prometheus.example.com:443:{cfg.node_ip('infra')}" in observed["command"]


def test_forward_auth_batch_uses_one_remote_probe(monkeypatch, tmp_path):
    cfg = Config(domain="example.com")
    calls = []

    def fake_ssh(_cfg, source_ip, command, **_kwargs):
        calls.append((source_ip, command))
        return (
            0,
            "__HOMELAB_FORWARD_0__\t302\thttps://auth.example.com/\n"
            "__HOMELAB_FORWARD_1__\t302\thttps://auth.example.com/\n",
            "",
        )

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    checks = _check_caddy_forward_auth_routes_batch(
        cfg,
        [
            ("grafana", "grafana.example.com", cfg.node_ip("media"), cfg.node_ip("infra")),
            ("prometheus", "prometheus.example.com", cfg.node_ip("media"), cfg.node_ip("infra")),
        ],
        tmp_path,
    )

    assert [check.passed for check in checks] == [True, True]
    assert len(calls) == 1
    assert calls[0][0] == cfg.node_ip("media")
    assert calls[0][1].count("curl -skI") == 2


def test_forward_auth_checks_follow_compiled_app_routes(monkeypatch):
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=True,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    observed: list[tuple[str, str]] = []
    observed_native: list[tuple[str, str, tuple[str, ...]]] = []

    def fake_batch(_cfg, entries, _root):
        checks = []
        for service, host, _source_ip, _caddy_ip in entries:
            observed.append((service, host))
            checks.append(VerifyCheck(service, "forward_auth", True, "ok"))
        return checks

    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_caddy_forward_auth_routes_batch", fake_batch)

    def fake_native_paths(_cfg, service, host, paths, _vm_ip, _root, **_kwargs):
        observed_native.append((service, host, paths))
        return [VerifyCheck(service, f"native_path:{path}", True, "ok") for path in paths]

    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_caddy_split_native_paths", fake_native_paths)

    checks = _check_forward_auth_routes(cfg, Path("."), vm_role="apps")

    assert {check.service for check in checks} == {"fmd-server", "gitea", "romm", "seaweedfs"}
    assert set(observed) == {
        ("fmd-server", "fmd.example.com"),
        ("gitea", "git.example.com"),
        ("romm", "romm.example.com"),
        ("seaweedfs", "files.example.com"),
    }
    fmd_route = next(route for route in compile_routes(cfg) if route.service == "fmd-server")
    romm_route = next(route for route in compile_routes(cfg) if route.service == "romm" and route.match is None)
    assert observed_native == [
        ("fmd-server", "fmd.example.com", fmd_route.auth.passthrough_paths),
        ("romm", "romm.example.com", romm_route.auth.passthrough_paths),
    ]


def test_cloudflare_public_dns_parity_detects_missing(monkeypatch):
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="cloudflare", public_ip="1.2.3.4", proxy_enabled=True),
        network=NetworkConfig(),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    secrets = {"CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ZONE_ID": "zone"}

    class FakeCF:
        def __init__(self, api_token="", zone_id=""):
            self._zone_id = zone_id

        def find_zone_id(self, _domain):
            return "zone"

        def list_all_managed_records(self):
            from toolkit.core.ops.dns import DNSRecord

            return [DNSRecord(name="auth.example.com", type="A", content="1.2.3.4", proxied=True)]

    monkeypatch.setattr("toolkit.core.ops.dns.CloudflareDNS", FakeCF)
    check = _check_cloudflare_public_dns_parity(cfg, secrets)
    assert not check.passed
    assert "missing" in check.detail.lower()


def test_cloudflare_public_dns_parity_fails_closed_without_token():
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="cloudflare", public_ip="1.2.3.4"),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )

    check = _check_cloudflare_public_dns_parity(cfg, {})

    assert not check.passed
    assert "cloudflare_api_token" in check.detail.lower()


def test_cloudflare_private_dns_parity_fails_closed_without_token():
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="cloudflare"),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )

    check = _check_private_fqdns_not_in_cloudflare(cfg, {})

    assert not check.passed
    assert "cloudflare_api_token" in check.detail.lower()


def test_cloudflare_dns_checks_are_not_applicable_for_other_provider():
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="route53"),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )

    public = _check_cloudflare_public_dns_parity(cfg, {})
    private = _check_private_fqdns_not_in_cloudflare(cfg, {})

    assert public.passed and "not applicable" in public.detail
    assert private.passed and "not applicable" in private.detail


def test_private_fqdns_not_in_cloudflare_allows_public_dns(monkeypatch):
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="cloudflare"),
        network=NetworkConfig(dns_public_access=True),
        services=ServicesConfig(
            management=True, media=False, cloud=False, notifications=False, email=False, security=False
        ),
    )
    secrets = {"CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ZONE_ID": "zone"}

    class FakeCF:
        def __init__(self, api_token="", zone_id=""):
            self._zone_id = zone_id

        def find_zone_id(self, _domain):
            return "zone"

        def list_records(self, record_type):
            from toolkit.core.ops.dns import DNSRecord

            if record_type == "A":
                return [DNSRecord(name="dns.example.com", type="A", content="1.2.3.4", proxied=False)]
            return []

    monkeypatch.setattr("toolkit.core.ops.dns.CloudflareDNS", FakeCF)
    check = _check_private_fqdns_not_in_cloudflare(cfg, secrets)
    assert check.passed


def test_private_fqdns_not_in_cloudflare_detects_leak(monkeypatch):
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(provider="cloudflare", public_ip="1.2.3.4"),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    secrets = {"CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ZONE_ID": "zone"}

    class FakeCF:
        def __init__(self, api_token="", zone_id=""):
            self._zone_id = zone_id

        def find_zone_id(self, _domain):
            return "zone"

        def list_records(self, record_type):
            from toolkit.core.ops.dns import DNSRecord

            if record_type == "A":
                return [DNSRecord(name="grafana.example.com", type="A", content="192.0.2.99")]
            return []

    monkeypatch.setattr("toolkit.core.ops.dns.CloudflareDNS", FakeCF)
    check = _check_private_fqdns_not_in_cloudflare(cfg, secrets)
    assert not check.passed
    assert "grafana" in check.detail


def test_mesh_client_access_fails_without_lan_route(monkeypatch):
    cfg = Config(
        domain="example.com",
        machines=machines_with_addresses(infra="10.10.10.10", media="10.10.10.11", apps="10.10.10.12"),
        services=ServicesConfig(management=True),
    )
    monkeypatch.setattr(HEADSCALE_MESH, "_mesh_routes_reachable", lambda _cfg: False)
    checks = HEADSCALE_MESH.controller_mesh_access_checks(cfg, Path("."))
    assert len(checks) == 1
    assert not checks[0].passed
    assert "no route" in checks[0].detail


def test_mesh_client_access_uses_probes(monkeypatch):
    cfg = Config(
        domain="example.com",
        machines=machines_with_addresses(infra="10.10.10.10", media="10.10.10.11", apps="10.10.10.12"),
        services=ServicesConfig(management=True, media=True),
    )
    monkeypatch.setattr(HEADSCALE_MESH, "probe_mesh_internal", lambda _cfg: [("infra-ssh", True, "10.10.10.10:22")])
    monkeypatch.setattr(HEADSCALE_MESH, "_mesh_routes_reachable", lambda _cfg: True)

    monkeypatch.setattr(HEADSCALE_MESH, "_mesh_private_https_check", lambda *_a, **_k: (True, "HTTP 302 -> auth"))
    checks = HEADSCALE_MESH.controller_mesh_access_checks(cfg, Path("."))
    labels = {c.check for c in checks}
    assert "infra-ssh" in labels
    assert "private-grafana" in labels


def test_mesh_private_forward_auth_requires_identity_redirect(monkeypatch):
    monkeypatch.setattr(
        HEADSCALE_MESH.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="HTTP/2 302\r\nlocation: https://auth.example.com/?rd=https://app.example.com/\r\n",
            stderr="",
        ),
    )

    ok, detail = HEADSCALE_MESH._mesh_private_https_check(
        "app.example.com", "10.10.10.10", "auth.example.com", "forward_auth"
    )

    assert ok is True
    assert detail == "HTTP 302 -> auth"


def test_mesh_private_oidc_accepts_application_login_surface(monkeypatch):
    monkeypatch.setattr(
        HEADSCALE_MESH.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="HTTP/2 200\r\n", stderr=""
        ),
    )

    ok, detail = HEADSCALE_MESH._mesh_private_https_check(
        "grafana.example.com", "10.10.10.10", "auth.example.com", "oidc"
    )

    assert ok is True
    assert detail == "HTTP 200 (oidc)"
