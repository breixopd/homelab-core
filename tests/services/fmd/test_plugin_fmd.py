from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.services import _reset_cache, get_service_plugin


def _plugin():
    _reset_cache()
    plugin = get_service_plugin("fmd-server")
    assert plugin is not None
    return plugin


def test_fmd_verify_checks_api_version_and_metrics(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def probe(_cfg, _ip, _container, url, **_kwargs):
        calls.append(url)
        if url.endswith("/metrics"):
            return 0, "# TYPE fmd_accounts gauge\nfmd_accounts 1\n"
        return 0, "0.16.0"

    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", probe)

    checks = {
        check.check: check for check in _plugin().verify(Config(domain="example.com"), {}, "10.10.10.12", tmp_path)
    }

    assert checks["api_version"].passed is True
    assert checks["metrics"].passed is True
    assert calls == ["http://localhost:8080/api/v1/version", "http://localhost:9100/metrics"]


def test_fmd_verify_reports_missing_container_as_not_deployed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_args, **_kwargs: False)

    checks = _plugin().verify(Config(domain="example.com"), {}, "10.10.10.12", tmp_path)

    assert len(checks) == 1
    assert checks[0].check == "deployment"
    assert checks[0].passed is False


def test_fmd_registration_token_is_a_managed_credential() -> None:
    credentials = _plugin().credentials(Config(domain="example.com"))

    assert len(credentials) == 1
    assert credentials[0].secret_key == "FMD_REGISTRATION_TOKEN"
    assert credentials[0].url_template == "https://fmd.example.com"


def test_fmd_wal_database_uses_consistent_manifest_owned_export() -> None:
    manifest = load_service_catalog().require("fmd-server")

    assert manifest.data_specs[0].snapshot is False
    assert len(manifest.backup_exports) == 1
    assert manifest.backup_exports[0].strategy == "sqlite"
    assert manifest.backup_exports[0].database_path == "fmd.sqlite"
