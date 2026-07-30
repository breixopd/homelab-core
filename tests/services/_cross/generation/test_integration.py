from __future__ import annotations

from pathlib import Path

from tests.helpers.machines import enabled_machines
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, ServicesConfig, ToolkitState, get_state, save_config
from toolkit.core.config.storage import config_path, env_path, secrets_path
from toolkit.core.generate.generate import generate_all
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.routes import compile_routes, service_is_enabled
from toolkit.core.secrets.secrets import (
    generate_all_secrets,
    get_required_secrets,
    load_secrets_plaintext,
    save_secrets_plaintext,
)

load_all()


def test_full_pipeline(tmp_path: Path):
    """End-to-end: config → secrets → generate → validate."""
    root = tmp_path

    # 1. Start uninitialized
    assert get_state(root) == ToolkitState.UNINITIALIZED

    # 2. Create config
    cfg = Config(domain="test.example.com", email="admin@test.example.com")
    save_config(cfg, config_path(root))
    assert get_state(root) == ToolkitState.CONFIG_ONLY

    # 3. Generate secrets
    specs = get_required_secrets(cfg)
    secrets = generate_all_secrets(specs)
    save_secrets_plaintext(secrets, secrets_path(root))

    # Verify secrets were generated
    loaded_secrets = load_secrets_plaintext(secrets_path(root))
    assert len(loaded_secrets["POSTGRES_PASSWORD"]) == 32
    assert "HEADSCALE_DB_PASSWORD" not in loaded_secrets
    assert "HEADSCALE_PRIVATE_KEY" not in loaded_secrets
    assert "PLEX_CLAIM" not in loaded_secrets  # jellyfin-only default
    assert loaded_secrets["SSO_USER_PASSWORD"]

    # 4. Generate .env files
    results = generate_all(root)
    assert get_state(root) == ToolkitState.READY

    # 5. Verify all VMs got .env files
    assert set(results.keys()) == {"infra", "media", "apps"}

    for vm in cfg.enabled_nodes:
        env_file = env_path(vm, root)
        assert env_file.exists()
        content = env_file.read_text()
        assert "BASE_DOMAIN=test.example.com" in content
        assert "TZ=" in content  # Auto-detected or from config

    # 6. Verify cross-VM networking
    infra_env = env_path("infra", root).read_text()
    apps_env = env_path("apps", root).read_text()
    media_env = env_path("media", root).read_text()
    assert "AUTHELIA_DB_HOST=postgres" in infra_env
    assert "GITEA_DB_HOST=10.10.10.10" in apps_env
    assert "_DB_HOST=" not in media_env


def test_partial_services_pipeline(tmp_path: Path):
    """Pipeline with only media + management enabled."""
    root = tmp_path
    cfg = Config(
        domain="minimal.example.com",
        services=ServicesConfig(
            management=True,
            media=True,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
        machines=enabled_machines("infra", "media"),
    )
    save_config(cfg, config_path(root))
    results = generate_all(root)

    assert set(results.keys()) == {"infra", "media"}
    assert "apps" not in results


def test_category_service_counts():
    """Every enabled manifest appears in exactly one category projection."""
    cfg = Config()
    cats = enabled_categories(cfg)
    projected = [service.name for category in cats for service in category.services(cfg)]
    expected = {
        manifest.name
        for manifest in load_service_catalog().manifests
        if manifest.category in cfg.enabled_categories and service_is_enabled(cfg, manifest)
    }

    assert len(projected) == len(set(projected))
    assert set(projected) == expected


def test_all_routes_have_valid_subdomains():
    """Every route has an explicit subdomain (service name default or apex '')."""
    for route in compile_routes(Config()):
        assert route.subdomain is not None, f"Route {route.service} is missing a subdomain"


def test_no_duplicate_routes():
    """Each compiled host has exactly one default route."""
    defaults = [route.host for route in compile_routes(Config()) if route.match is None]
    assert len(defaults) == len(set(defaults))


def test_secrets_cover_all_categories():
    """Every enabled category's secrets are included."""
    cfg = Config()
    specs = get_required_secrets(cfg)
    names = {s.name for s in specs}
    assert "POSTGRES_PASSWORD" in names  # core
    assert "SONARR_API_KEY" in names  # media
    assert "NEXTCLOUD_OIDC_CLIENT_SECRET" in names  # cloud
    assert "GITEA_DB_PASSWORD" in names  # cloud (Gitea on apps)
    assert "HEADSCALE_OIDC_CLIENT_SECRET" in names  # security
    assert "HEADSCALE_DB_PASSWORD" not in names
    assert "SPOTIFY_CLIENT_ID" in names  # music sync
