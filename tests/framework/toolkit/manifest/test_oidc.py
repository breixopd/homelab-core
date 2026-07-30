from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.oidc import OIDCCompilationError, compile_oidc_clients
from toolkit.core.manifest.schema import ServiceManifest


def _service(
    name: str,
    *,
    category: str = "cloud",
    client_id: str | None = None,
    extra_oidc: dict[str, object] | None = None,
) -> ServiceManifest:
    identifier = client_id or name
    secret = f"{name.upper().replace('-', '_')}_OIDC_SECRET"
    oidc: dict[str, object] = {
        "client_id": identifier,
        "secret_env_var": secret,
        "redirect_uris": [f"https://{name}.{{domain}}/oidc/callback"],
    }
    oidc.update(extra_oidc or {})
    return ServiceManifest.model_validate(
        {
            "name": name,
            "label": name.title(),
            "description": f"{name} service",
            "icon": "box",
            "category": category,
            "placement": "apps",
            "priority": 20,
            "routes": [
                {
                    "upstream": f"{name}:8080",
                    "exposure": "private",
                    "auth": {"mode": "oidc"},
                }
            ],
            "oidc": oidc,
            "required_secrets": [
                {"name": secret, "tier": "generated", "description": "OIDC client secret", "rotation": "restart"}
            ],
        }
    )


def test_compile_oidc_clients_uses_enabled_manifests_and_expands_domain() -> None:
    cloud = _service("photos", client_id="immich")
    security = _service("headscale", category="security")
    cfg = Config(
        domain="home.test",
        services=ServicesConfig(cloud=True, security=False),
    )

    clients = compile_oidc_clients(
        cfg,
        ServiceCatalog((cloud, security)),
        {"PHOTOS_OIDC_SECRET": "cloud-secret"},
    )

    assert [client.client_id for client in clients] == ["immich"]
    assert clients[0].redirect_uris == ("https://photos.home.test/oidc/callback",)
    assert PasswordHasher().verify(clients[0].secret_hash, "cloud-secret") is True


def test_compile_oidc_clients_reuses_a_valid_secret_hash() -> None:
    service = _service("example")
    catalog = ServiceCatalog((service,))
    cfg = Config(domain="home.test")
    first = compile_oidc_clients(cfg, catalog, {"EXAMPLE_OIDC_SECRET": "secret"})[0]
    second = compile_oidc_clients(
        cfg,
        catalog,
        {"EXAMPLE_OIDC_SECRET": "secret"},
        existing_hashes={"EXAMPLE_OIDC_SECRET": first.secret_hash},
    )[0]

    assert second.secret_hash == first.secret_hash


def test_compile_oidc_clients_preserves_typed_client_capabilities() -> None:
    headscale = _service(
        "headscale",
        category="security",
        extra_oidc={
            "authorization_policy": "two_factor",
            "require_pkce": True,
            "pkce_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_basic",
            "response_types": ["code"],
            "grant_types": ["authorization_code"],
        },
    )

    client = compile_oidc_clients(
        Config(domain="home.test"),
        ServiceCatalog((headscale,)),
        {"HEADSCALE_OIDC_SECRET": "secret"},
    )[0]

    assert client.authorization_policy == "two_factor"
    assert client.require_pkce is True
    assert client.pkce_challenge_method == "S256"
    assert client.token_endpoint_auth_method == "client_secret_basic"


def test_compile_oidc_clients_fails_closed_when_secret_is_missing() -> None:
    service = _service("example")

    with pytest.raises(OIDCCompilationError, match="EXAMPLE_OIDC_SECRET"):
        compile_oidc_clients(Config(domain="home.test"), ServiceCatalog((service,)), {})
