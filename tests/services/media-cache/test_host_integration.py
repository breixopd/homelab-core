"""Service-owned tests for media-cache managed-host reconciliation."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

from toolkit.core.config.config import Config, ExternalHost, SSHConfig, save_config, save_local_config
from toolkit.core.config.storage import config_path
from toolkit.core.infra.hosts import reconcile_host_integrations, remove_host

render_external_media_cache_config = import_module(
    "toolkit.services.media-cache.client"
).render_external_media_cache_config


def _config(root: Path, *, key_file: Path, hosts: list[ExternalHost] | None = None) -> None:
    cfg = Config(
        domain="example.com",
        ssh=SSHConfig(auth_method="key", key_file=str(key_file)),
        external_hosts=hosts or [],
    )
    save_config(cfg, config_path(root))
    # SSH key paths are local-only and are intentionally stripped from tracked config.
    save_local_config(cfg, root)


def _host(name: str = "nas-01") -> ExternalHost:
    return ExternalHost(
        name=name,
        ip="10.0.0.9",
        services=["media-cache"],
        integrations={"media-cache": {"path": "/srv/cache"}},
    )


def test_reconcile_projects_desired_pool_and_does_not_call_service_mutation_api(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("private-key\n", encoding="utf-8")
    host = _host()
    _config(tmp_path, key_file=key, hosts=[host])
    monkeypatch.setattr("toolkit.core.ops.dns.sync_external_hosts_dns", lambda *_a, **_k: {})

    result = reconcile_host_integrations(tmp_path, host)

    assert result.ok is True
    assert (tmp_path / "config" / "rclone" / "rclone.conf").exists()
    assert "ext-nas-01:/srv/cache" in (tmp_path / "config" / "rclone" / "rclone.conf").read_text()


def test_reconcile_reports_projection_errors(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "missing-ed25519"
    host = _host()
    _config(tmp_path, key_file=key, hosts=[host])
    monkeypatch.setattr("toolkit.core.ops.dns.sync_external_hosts_dns", lambda *_a, **_k: {})

    result = reconcile_host_integrations(tmp_path, host)

    assert result.ok is False
    assert len(result.errors) == 1
    assert "SSH key file not found" in result.errors[0]


def test_remove_host_removes_only_its_projected_remote(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("private-key\n", encoding="utf-8")
    first = _host("nas-01")
    second = _host("nas-02")
    _config(tmp_path, key_file=key, hosts=[first, second])
    monkeypatch.setattr("toolkit.core.ops.dns.remove_external_host_dns", lambda *_a, **_k: None)
    render_external_media_cache_config(tmp_path)
    assert remove_host(tmp_path, "nas-01") is True

    config = (tmp_path / "config" / "rclone" / "rclone.conf").read_text()
    assert "ext-nas-01" not in config
    assert "ext-nas-02:/srv/cache" in config
