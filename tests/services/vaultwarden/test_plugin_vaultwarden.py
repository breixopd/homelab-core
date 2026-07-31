"""Unit tests for vaultwarden plugin verify()."""

from __future__ import annotations

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services import RuntimeEnvironmentContext
from toolkit.services.sdk import VerifyCheck


def _plugin():
    return load_plugin("vaultwarden").VaultwardenPlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(cloud=True, management=True))


def test_runtime_environment_hashes_plaintext_admin_token(tmp_path) -> None:
    context = RuntimeEnvironmentContext(
        config=_cfg(),
        node="apps",
        root=tmp_path,
        secrets={"VAULTWARDEN_ADMIN_TOKEN": "plain-token"},
        previous={},
    )

    value = _plugin().runtime_environment(context)["VAULTWARDEN_ADMIN_TOKEN"]

    assert value.startswith("$argon2id$")


def test_runtime_environment_reuses_matching_admin_hash(tmp_path) -> None:
    plugin = _plugin()
    first = plugin.runtime_environment(
        RuntimeEnvironmentContext(_cfg(), "apps", tmp_path, {"VAULTWARDEN_ADMIN_TOKEN": "plain-token"}, {})
    )["VAULTWARDEN_ADMIN_TOKEN"]

    second = plugin.runtime_environment(
        RuntimeEnvironmentContext(
            _cfg(),
            "apps",
            tmp_path,
            {"VAULTWARDEN_ADMIN_TOKEN": "plain-token"},
            {"VAULTWARDEN_ADMIN_TOKEN": first},
        )
    )["VAULTWARDEN_ADMIN_TOKEN"]

    assert second == first


def test_status_uses_only_the_bounded_readiness_probe(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def docker_curl(_cfg, _vm_ip, _container, url, **kwargs):
        calls.append({"url": url, **kwargs})
        return 0, "OK"

    monkeypatch.setattr("toolkit.services.sdk.docker_curl", docker_curl)

    result = _plugin().status(_cfg(), {"VAULTWARDEN_MASTER_PASSWORD": "must-not-be-used"}, tmp_path)

    assert result == {"readiness": 1}
    assert calls == [
        {
            "url": "http://localhost/alive",
            "root": tmp_path,
            "timeout": 10,
        }
    ]


def test_management_manifest_contains_non_secret_recovery_guidance() -> None:
    from toolkit.core.manifest.catalog import load_service_catalog

    management = next(
        manifest for manifest in load_service_catalog().manifests if manifest.name == "vaultwarden"
    ).management
    panels = {panel.id: panel for panel in management.panels}

    assert set(panels) == {"account-access", "readiness-recovery"}
    recovery_text = " ".join(item.value for panel in panels.values() for item in panel.items).lower()
    assert "master password" in recovery_text
    assert "persisted encryption keys" in recovery_text
    assert all("VAULTWARDEN_" not in item.value for panel in panels.values() for item in panel.items)
    assert [(metric.key, metric.field) for metric in management.metrics] == [("readiness", "readiness")]


class TestVaultwardenVerify:
    def test_alive_and_admin_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.oidc_check_env_issuer",
            lambda *_a, **_k: [
                VerifyCheck("vaultwarden", "oidc_issuer", True, "https://auth.example.com"),
                VerifyCheck("vaultwarden", "oidc_token_route", True, "ok"),
            ],
        )

        def docker_curl(_cfg, _vm_ip, _container, url, **_kwargs):
            if url.endswith("/accounts/prelogin"):
                return 0, '{"Kdf":0,"KdfIterations":600000}'
            if url.endswith("/connect/token"):
                return 0, '{"access_token":"verified"}'
            return 0, "OK"

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", docker_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.vaultwarden_admin_session",
            lambda *_a, **_k: {"session": "x"},
        )
        monkeypatch.setattr("toolkit.services.sdk.vaultwarden_fetch_kdf", lambda *_a, **_k: object())
        monkeypatch.setattr("toolkit.services.sdk.vaultwarden_login_access_token", lambda *_a, **_k: "access-token")

        checks = {
            c.check: c
            for c in _plugin().verify(
                _cfg(),
                {
                    "VAULTWARDEN_ADMIN_TOKEN": "tok",
                    "VAULTWARDEN_MASTER_PASSWORD": "chosen-master",
                    "SSO_CLIENT_SECRET": "s",
                },
                "10.10.10.12",
                tmp_path,
            )
        }
        assert checks["alive"].passed
        assert checks["admin_session"].passed
        assert checks["owner_login"].passed
        assert checks["oidc_issuer"].passed

    def test_skips_alive_on_localhost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "toolkit.services.sdk.oidc_check_env_issuer",
            lambda *_a, **_k: [],
        )
        checks = {c.check: c for c in _plugin().verify(_cfg().__class__(domain="localhost"), {}, "127.0.0.1", tmp_path)}
        assert checks["alive"].passed and "localhost" in checks["alive"].detail
