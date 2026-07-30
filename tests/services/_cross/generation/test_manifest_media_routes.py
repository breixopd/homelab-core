from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import clear_catalog_cache, load_service_catalog
from toolkit.core.manifest.routes import compile_routes
from toolkit.core.manifest.schema import ServiceManifest

_SERVICES = Path(__file__).resolve().parents[4] / "toolkit" / "services"


def _manifest(name: str) -> ServiceManifest:
    raw = yaml.safe_load((_SERVICES / name / "service.yaml").read_text(encoding="utf-8"))
    return ServiceManifest.model_validate(raw)


def _media_routes(
    *,
    server: str = "jellyfin",
    vpn: bool = True,
    tdarr: bool = True,
    service_settings: dict | None = None,
) -> dict:
    clear_catalog_cache()
    settings = {
        "media-library": {"server": server},
        "gluetun": {"enabled": vpn},
        "tdarr": {"enabled": tdarr},
    }
    settings.update(service_settings or {})
    cfg = Config(
        domain="home.example",
        service_settings=settings,
    )
    return {route.service: route for route in compile_routes(cfg, load_service_catalog()) if route.category == "media"}


def test_media_server_routes_follow_typed_configuration_predicates() -> None:
    jellyfin = _media_routes(server="jellyfin")
    plex = _media_routes(server="plex")
    both = _media_routes(server="both")

    assert "jellyfin" in jellyfin and "plex" not in jellyfin and "tautulli" not in jellyfin
    assert "jellyfin" not in plex and "plex" in plex and "tautulli" in plex
    assert {"jellyfin", "plex", "tautulli"} <= both.keys()
    assert jellyfin["jellyfin"].auth.mode == "native"
    assert plex["plex"].auth.mode == "native"


def test_qbittorrent_route_selects_the_vpn_compose_target() -> None:
    vpn = _media_routes(vpn=True)["qbittorrent"]
    direct = _media_routes(vpn=False)["qbittorrent"]

    assert (vpn.upstream, vpn.compose_service) == ("gluetun:8080", "qbittorrent-vpn")
    assert (direct.upstream, direct.compose_service) == ("qbittorrent:8080", "qbittorrent")
    assert vpn.exposure == "private"
    assert vpn.auth.mode == "forward_auth"


def test_optional_media_services_compile_only_when_enabled() -> None:
    enabled = _media_routes(tdarr=True)
    disabled = _media_routes(tdarr=False)
    plugin_disabled = _media_routes(
        service_settings={"media-cache": {"enabled": False}, "music-sync": {"enabled": False}}
    )

    assert "tdarr" in enabled
    assert "tdarr" not in disabled
    assert "media-cache" not in plugin_disabled
    assert "music-sync" not in plugin_disabled


def test_media_routes_have_deliberate_exposure_and_authentication() -> None:
    expected = {
        "bazarr": ("private", "forward_auth"),
        "jellyfin": ("public", "native"),
        "music-sync": ("private", "forward_auth"),
        "navidrome": ("public", "forward_auth"),
        "plex": ("public", "native"),
        "prowlarr": ("private", "forward_auth"),
        "qbittorrent": ("private", "forward_auth"),
        "radarr": ("private", "forward_auth"),
        "seerr": ("public", "forward_auth"),
        "sonarr": ("private", "forward_auth"),
        "tautulli": ("private", "forward_auth"),
        "tdarr": ("private", "forward_auth"),
    }
    for name, (exposure, auth_mode) in expected.items():
        route = next(route for route in _manifest(name).routes if route.match is None)
        assert (route.exposure, route.auth.mode) == (exposure, auth_mode), name


def test_navidrome_keeps_subsonic_and_share_protocols_on_native_auth() -> None:
    manifest = _manifest("navidrome")

    assert manifest.oidc is None
    assert [(route.match.kind, route.match.paths) for route in manifest.routes if route.match is not None] == [
        ("prefix", ("/rest/",)),
        ("prefix", ("/share/",)),
    ]
    assert all(route.auth.mode == "native" for route in manifest.routes if route.match is not None)
    assert manifest.routes[-1].auth.mode == "forward_auth"
