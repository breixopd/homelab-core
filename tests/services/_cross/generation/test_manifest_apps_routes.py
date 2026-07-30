from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.manifest.schema import ServiceManifest

_SERVICES = Path(__file__).resolve().parents[4] / "toolkit" / "services"


def _manifest(name: str) -> ServiceManifest:
    raw = yaml.safe_load((_SERVICES / name / "service.yaml").read_text(encoding="utf-8"))
    return ServiceManifest.model_validate(raw)


def test_apps_email_and_security_routes_have_explicit_policy() -> None:
    expected_defaults = {
        "fmd-server": ("public", "split"),
        "gitea": ("public", "forward_auth"),
        "immich-server": ("public", "oidc"),
        "nextcloud": ("public", "oidc"),
        "vaultwarden": ("public", "oidc"),
        "roundcube": ("public", "forward_auth"),
        "headscale": ("public", "oidc"),
        "wazuh-dashboard": ("private", "forward_auth"),
    }
    for name, (exposure, auth_mode) in expected_defaults.items():
        defaults = [route for route in _manifest(name).routes if route.match is None]
        assert len(defaults) == 1, name
        assert defaults[0].exposure == exposure, name
        assert defaults[0].auth.mode == auth_mode, name

    seaweed = _manifest("seaweedfs")
    assert {(route.subdomain, route.exposure, route.auth.mode) for route in seaweed.routes} == {
        ("s3", "public", "native"),
        ("files", "public", "forward_auth"),
    }


def test_gitea_registry_protocol_route_precedes_authenticated_default() -> None:
    routes = _manifest("gitea").routes
    registry_index, registry = next(
        (index, route)
        for index, route in enumerate(routes)
        if route.match is not None and route.match.kind == "prefix" and route.match.paths == ("/v2/",)
    )
    default_index, default = next((index, route) for index, route in enumerate(routes) if route.match is None)

    assert registry_index < default_index
    assert registry.auth.mode == "native"
    assert default.auth.mode == "forward_auth"


def test_apps_oidc_clients_are_complete_and_secret_scoped() -> None:
    expected = {
        "immich-server": (
            "immich",
            "IMMICH_OIDC_CLIENT_SECRET",
            (
                "https://photos.{domain}/auth/login",
                "https://photos.{domain}/user-settings",
                "https://photos.{domain}/api/oauth/mobile-redirect",
                "app.immich:///oauth-callback",
            ),
        ),
        "nextcloud": (
            "nextcloud",
            "NEXTCLOUD_OIDC_CLIENT_SECRET",
            ("https://cloud.{domain}/apps/oidc_login/oidc",),
        ),
        "vaultwarden": (
            "vaultwarden",
            "VAULTWARDEN_SSO_CLIENT_SECRET",
            ("https://vault.{domain}/identity/connect/oidc-signin",),
        ),
        "headscale": (
            "headscale",
            "HEADSCALE_OIDC_CLIENT_SECRET",
            ("https://vpn.{domain}/oidc/callback",),
        ),
    }

    for name, (client_id, secret_name, redirect_uris) in expected.items():
        manifest = _manifest(name)
        assert manifest.oidc is not None
        assert manifest.oidc.client_id == client_id
        assert manifest.oidc.secret_env_var == secret_name
        assert manifest.oidc.redirect_uris == redirect_uris
        assert secret_name in {secret.name for secret in manifest.required_secrets}


def test_vaultwarden_admin_is_denied_by_typed_route_policy() -> None:
    route = next(route for route in _manifest("vaultwarden").routes if route.match is None)

    assert {(match.kind, match.paths) for match in route.deny} == {
        ("exact", ("/admin",)),
        ("prefix", ("/admin/",)),
    }
    assert any(header.name == "Content-Security-Policy" for header in route.response_headers)


def test_fmd_exposes_only_current_phone_protocol_paths_without_authelia() -> None:
    manifest = _manifest("fmd-server")
    route = manifest.routes[0]
    upstream_v1_endpoints = {
        "command",
        "location",
        "locations",
        "locations/delete",
        "locationDataSize",
        "picture",
        "pictures",
        "pictures/delete",
        "pictureSize",
        "key",
        "pubKey",
        "device",
        "password",
        "push",
        "salt",
        "requestAccess",
        "tileServerUrl",
        "version",
    }
    expected_paths = {"/version", "/version/"}
    for endpoint in upstream_v1_endpoints:
        expected_paths.update({f"/api/v1/{endpoint}", f"/api/v1/{endpoint}/"})

    assert route.auth.mode == "split"
    assert route.request_body_max_mb == 15
    assert set(route.auth.passthrough_paths) == expected_paths
    assert manifest.oidc is None
