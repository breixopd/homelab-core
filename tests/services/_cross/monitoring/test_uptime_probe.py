from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.ops.uptime_probe import _build_probe_urls, probe_public_endpoints


def test_probe_urls_follow_public_manifests_and_service_health_paths():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            management=True,
            media=True,
            cloud=True,
            notifications=True,
            email=False,
            security=False,
        ),
    )

    probes = dict(_build_probe_urls(cfg))

    assert probes == {
        "authelia": "https://auth.example.com/.well-known/openid-configuration",
        "fmd-server": "https://fmd.example.com/api/v1/version",
        "gitea": "https://git.example.com/api/healthz",
        "homelab-ui": "https://homelab.example.com/health",
        "immich-server": "https://photos.example.com/api/server/ping",
        "jellyfin": "https://jellyfin.example.com/health",
        "nextcloud": "https://cloud.example.com/status.php",
        "ntfy": "https://ntfy.example.com/v1/health",
        "romm": "https://romm.example.com/api/heartbeat",
        "uptime-kuma": "https://status.example.com/",
        "vaultwarden": "https://vault.example.com/alive",
    }
    assert "grafana" not in probes


def test_public_probe_rejects_missing_route(monkeypatch, tmp_path):
    cfg = Config(domain="example.com")
    monkeypatch.setattr(
        "toolkit.core.ops.uptime_probe._build_probe_urls",
        lambda _cfg: [("authelia", "https://auth.example.com/.well-known/openid-configuration")],
    )

    class Response:
        status_code = 404

    monkeypatch.setattr("toolkit.core.ops.uptime_probe.httpx.get", lambda *_args, **_kwargs: Response())

    [result] = probe_public_endpoints(cfg, tmp_path)

    assert not result.ok
    assert result.status_code == 404


def test_custom_service_contributes_public_probe_without_core_change(tmp_path: Path) -> None:
    service = tmp_path / "example"
    service.mkdir()
    (service / "service.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example",
                "label": "Example",
                "description": "Custom service",
                "icon": "box",
                "category": "cloud",
                "placement": "apps",
                "priority": 50,
                "routes": [
                    {
                        "subdomain": "status",
                        "upstream": "example:8080",
                        "exposure": "public",
                        "auth": {"mode": "native"},
                    }
                ],
                "health": {"public_probe_path": "/ready"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (service / "compose.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "example": {
                        "image": "example/service:1@sha256:" + "a" * 64,
                        "logging": {
                            "driver": "json-file",
                            "options": {"max-size": "10m", "max-file": "3"},
                        },
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    probes = _build_probe_urls(
        Config(domain="example.com", services={"cloud": True}),
        load_service_catalog(tmp_path),
    )

    assert probes == [("example", "https://status.example.com/ready")]
