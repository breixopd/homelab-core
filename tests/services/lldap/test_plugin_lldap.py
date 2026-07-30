"""Unit tests for lldap plugin verify()."""

from __future__ import annotations

import json

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    return load_plugin("lldap").LldapPlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(management=True, cloud=True))


class TestLldapVerify:
    def test_skips_on_localhost(self, tmp_path):
        checks = _plugin().verify(_cfg().__class__(domain="localhost"), {}, "10.10.10.10", tmp_path)
        assert checks[0].passed and "localhost" in checks[0].detail

    def test_health_bind_and_graphql(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        bind_calls = []

        def fake_bind(*args, **kwargs):
            bind_calls.append((args, kwargs))
            return 0, "dn: cn=admin,ou=people,dc=example,dc=com"

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/health"):
                return 0, "OK"
            if url.endswith("/auth/simple/login"):
                assert _kw["body"] == json.dumps({"username": "admin", "password": "admin"})
                return 0, json.dumps({"token": "tok"})
            if url.endswith("/api/graphql"):
                assert _kw["headers"]["Authorization"] == "Bearer tok"
                return 0, json.dumps({"data": {"groups": [{"displayName": "homelab-users"}]}})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.ldap_bind_search_on_vm", fake_bind)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)

        checks = {
            c.check: c
            for c in _plugin().verify(
                _cfg(),
                {"LLDAP_BIND_PASSWORD": "test-only-bind-password", "LLDAP_ADMIN_PASSWORD": "admin"},
                "10.10.10.10",
                tmp_path,
            )
        }
        assert checks["check_health"].passed
        assert checks["ldap_bind"].passed
        assert checks["graphql_groups"].passed
        assert checks["base_dn"].passed
        bind_args, bind_kwargs = bind_calls[0]
        assert bind_args[1] == "10.10.10.10"
        assert bind_kwargs["bind_password"] == "test-only-bind-password"
        assert bind_kwargs["search_filter"] == "(uid=admin)"
