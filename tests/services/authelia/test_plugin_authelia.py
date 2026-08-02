"""Unit tests for authelia plugin verify()."""

from __future__ import annotations

import json
import stat

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, NotificationsConfig, ServicesConfig, SMTPNotificationConfig
from toolkit.core.generate.artifacts import ArtifactGenerationContext
from toolkit.core.ops.notifications import SMTPProbeResult
from toolkit.services import get_service_plugin
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
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_args, **_kwargs: (0, "220 mail.example.com ESMTP\n250 mail.example.com"),
        )
        monkeypatch.setattr(
            "toolkit.core.ops.notifications.probe_smtp_transport",
            lambda _transport: SMTPProbeResult(True, "ready", "verified"),
        )
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
        assert checks["config_validate"].passed
        assert checks["storage_encryption"].passed
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
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, "220 mail.example.com ESMTP\n250 mail.example.com"),
        )
        monkeypatch.setattr("toolkit.services.sdk.ldap_bind_search_on_vm", lambda *_a, **_k: (1, "bind failed"))
        monkeypatch.setattr(
            "toolkit.core.ops.notifications.probe_smtp_transport",
            lambda _transport: SMTPProbeResult(True, "ready", "verified"),
        )

        checks = {
            c.check: c for c in _plugin().verify(_cfg(), {"LLDAP_BIND_PASSWORD": "bind"}, "10.10.10.10", tmp_path)
        }
        assert checks["notifier_smtp"].passed is True
        assert checks["oidc_jwks"].passed is False


def test_authelia_readiness_commands_are_static_and_do_not_expose_storage_key(tmp_path, monkeypatch):
    discovery = {"issuer": "https://auth.example.com", "jwks_uri": "http://localhost:9091/jwks.json"}
    calls = []
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
    monkeypatch.setattr("toolkit.services.sdk.authelia_oidc_discovery", lambda *_a, **_k: (discovery, ""))
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (1, "unavailable"))
    monkeypatch.setattr("toolkit.services.sdk.ldap_bind_search_on_vm", lambda *_a, **_k: (1, "bind failed"))
    monkeypatch.setattr(
        "toolkit.core.ops.notifications.probe_smtp_transport",
        lambda _transport: SMTPProbeResult(True, "ready", "verified"),
    )

    storage_key = "test-only-authelia-storage-key"

    def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return (1, f"AUTHELIA_STORAGE_KEY={storage_key}")

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    checks = {
        c.check: c
        for c in _plugin().verify(
            _cfg(),
            {
                "LLDAP_BIND_PASSWORD": "test-only-bind-password",
                "AUTHELIA_STORAGE_KEY": storage_key,
            },
            "10.10.10.10",
            tmp_path,
        )
    }

    assert checks["config_validate"].passed is False
    assert checks["storage_encryption"].passed is False
    assert checks["storage_encryption"].detail == "storage_encryption failed (rc=1)"
    assert storage_key not in checks["storage_encryption"].detail
    assert len(calls) == 3
    assert all(kwargs["timeout"] == 10 for _args, kwargs in calls)
    assert calls[0][1].get("user", "") == ""
    assert all(kwargs["user"] == "1000:1000" for _args, kwargs in calls[1:])
    assert all("AUTHELIA_STORAGE_KEY" not in repr((args, kwargs)) for args, kwargs in calls)
    assert calls[0][0][2] == [
        "sh",
        "-c",
        "printf 'EHLO authelia\\r\\nQUIT\\r\\n' | nc -w 5 mailserver 25",
    ]
    assert calls[1][0][2] == [
        "sh",
        "-c",
        "exec authelia config validate --config /config/configuration.yml >/dev/null 2>&1",
    ]
    assert calls[2][0][2] == [
        "sh",
        "-c",
        "exec authelia storage encryption check --config /config/configuration.yml >/dev/null 2>&1",
    ]


def test_authelia_uses_docker_dns_for_colocated_lldap() -> None:
    assert _authelia_ldap_url(_cfg()) == "ldap://lldap:3890"


def test_authelia_heal_delegates_to_service_owned_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.services.authelia.bootstrap.heal_authelia", lambda root: [f"healed {root.name}"])

    assert _plugin().heal(_cfg(), tmp_path) == [f"healed {tmp_path.name}"]


def test_authelia_external_smtp_uses_file_secret_and_verified_tls_config(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "toolkit.core.manifest.oidc.compile_oidc_clients",
        lambda *_args, **_kwargs: [],
    )
    cfg = Config(
        domain="example.com",
        notifications=NotificationsConfig(
            smtp=SMTPNotificationConfig(
                mode="external",
                host="smtp.gmail.com",
                port=587,
                starttls=True,
                username="operator@gmail.com",
                password_secret="OPERATOR_SMTP_PASSWORD",
                from_address="operator@gmail.com",
            )
        ),
    )
    plugin = get_service_plugin("authelia")
    assert plugin is not None
    context = ArtifactGenerationContext(
        cfg,
        tmp_path,
        {
            "AUTHELIA_OIDC_HMAC_SECRET": "test-hmac-secret",
            "OPERATOR_SMTP_PASSWORD": "gmail-app-password-canary",
        },
        plugin.manifest,
    )

    plugin.generate_artifacts(context)
    context.finish()

    rendered = (tmp_path / "generated/authelia/configuration.yml").read_text()
    password_file = tmp_path / "generated/authelia/smtp-password"
    assert 'address: "submission://smtp.gmail.com:587"' in rendered
    assert 'username: "operator@gmail.com"' in rendered
    assert 'sender: "Authelia <operator@gmail.com>"' in rendered
    assert "disable_startup_check: false" in rendered
    assert "gmail-app-password-canary" not in rendered
    assert password_file.read_text() == "gmail-app-password-canary"
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert (tmp_path / "generated/authelia/notifier.env").read_text() == (
        "AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE=/config/smtp-password\n"
    )


def test_authelia_disabled_smtp_keeps_single_filesystem_notifier(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "toolkit.core.manifest.oidc.compile_oidc_clients",
        lambda *_args, **_kwargs: [],
    )
    cfg = Config(
        domain="example.com",
        notifications=NotificationsConfig(
            smtp=SMTPNotificationConfig(mode="disabled"),
        ),
    )
    plugin = get_service_plugin("authelia")
    assert plugin is not None
    context = ArtifactGenerationContext(
        cfg,
        tmp_path,
        {"AUTHELIA_OIDC_HMAC_SECRET": "test-hmac-secret"},
        plugin.manifest,
    )

    plugin.generate_artifacts(context)
    context.finish()

    rendered = (tmp_path / "generated/authelia/configuration.yml").read_text()
    assert "filesystem:" in rendered
    assert "smtp:" not in rendered
    assert (tmp_path / "generated/authelia/smtp-password").read_text() == ""
    assert (tmp_path / "generated/authelia/notifier.env").read_text() == ""
