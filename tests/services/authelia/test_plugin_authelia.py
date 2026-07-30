"""Unit tests for authelia plugin verify()."""

from __future__ import annotations

import json

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services.authelia.plugin import _authelia_ldap_url


def _plugin():
    return load_plugin("authelia").AutheliaPlugin()


def _cfg(*, domain: str = "example.com") -> Config:
    return Config(domain=domain, services=ServicesConfig(management=True, cloud=True))


class TestAutheliaVerify:
    def test_skips_on_localhost(self, tmp_path):
        checks = _plugin().verify(_cfg(domain="localhost"), {}, "10.10.10.10", tmp_path)
        assert len(checks) == 1
        assert checks[0].passed and "localhost" in checks[0].detail

    def test_api_health_and_jwks(self, tmp_path, monkeypatch):
        discovery = {
            "issuer": "https://auth.example.com",
            "userinfo_endpoint": "https://auth.example.com/api/oidc/userinfo",
            "jwks_uri": "http://localhost:9091/jwks.json",
        }
        jwks = {"keys": [{"kty": "RSA", "kid": "1"}]}

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.authelia_oidc_discovery",
            lambda *_a, **_k: (discovery, ""),
        )

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/api/health"):
                return 0, "OK"
            if "jwks" in url:
                return 0, json.dumps(jwks)
            return 255, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        bind_calls = []

        def fake_bind(*args, **kwargs):
            bind_calls.append((args, kwargs))
            return 0, "dn: cn=ldap-bind,ou=people,dc=example,dc=com"

        monkeypatch.setattr("toolkit.services.sdk.ldap_bind_search_on_vm", fake_bind)

        checks = {
            c.check: c
            for c in _plugin().verify(
                _cfg(), {"LLDAP_BIND_PASSWORD": "test-only-bind-password"}, "10.10.10.10", tmp_path
            )
        }
        assert checks["api_health"].passed
        assert checks["oidc_jwks"].passed
        assert "1 key" in checks["oidc_jwks"].detail
        assert checks["ldap_bind"].passed
        assert bind_calls[0][1]["bind_password"] == "test-only-bind-password"
        assert bind_calls[0][1]["search_filter"] == "(uid=ldap-bind)"

    def test_jwks_fails_when_no_keys(self, tmp_path, monkeypatch):
        discovery = {"issuer": "https://auth.example.com", "jwks_uri": "http://localhost:9091/jwks.json"}

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.authelia_oidc_discovery",
            lambda *_a, **_k: (discovery, ""),
        )
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, '{"keys": []}'))
        monkeypatch.setattr("toolkit.services.sdk.ldap_bind_search_on_vm", lambda *_a, **_k: (1, "bind failed"))

        checks = {
            c.check: c for c in _plugin().verify(_cfg(), {"LLDAP_BIND_PASSWORD": "bind"}, "10.10.10.10", tmp_path)
        }
        assert checks["oidc_jwks"].passed is False


def test_authelia_uses_docker_dns_for_colocated_lldap() -> None:
    assert _authelia_ldap_url(_cfg()) == "ldap://lldap:3890"


def test_authelia_heal_delegates_to_service_owned_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.services.authelia.bootstrap.heal_authelia", lambda root: [f"healed {root.name}"])

    assert _plugin().heal(_cfg(), tmp_path) == [f"healed {tmp_path.name}"]
