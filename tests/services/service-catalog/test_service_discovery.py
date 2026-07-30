"""Tests for the new flat services/ directory — zero-code service addition.

Phase A: proves the architecture works end-to-end with Grafana as the
proof-of-concept. The discovery scans toolkit/services/<name>/, finds the
ServicePlugin subclass in plugin.py, and the framework can call
compose_service() + env_vars() on it generically.
"""

from __future__ import annotations

import pytest
import yaml


class TestServiceDiscovery:
    """The framework discovers services by scanning toolkit/services/ dirs."""

    def test_discovers_grafana_plugin(self):
        """Scanning toolkit/services/ finds the GrafanaPlugin."""
        from toolkit.services import discover_service_plugins

        plugins = discover_service_plugins()
        services = {p.service for p in plugins}
        assert "grafana" in services, f"grafana not found in {services}"

    def test_grafana_plugin_carries_category_and_placement(self):
        """The plugin knows its category and placement from its manifest."""
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        assert grafana.category == "management"
        assert grafana.placement == "control"


class TestGrafanaComposeService:
    """plugin.compose_service(cfg) returns the docker-compose service block."""

    def test_returns_compose_dict_with_image(self):
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        compose = grafana.compose_service(cfg=None)
        assert "image" in compose
        assert "grafana/grafana" in compose["image"]

    def test_compose_has_oidc_env_vars(self):
        """The compose block carries the OIDC env vars that generate.py used to inject."""
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        compose = grafana.compose_service(cfg=None)
        env = compose.get("environment", {})
        assert "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET" in env
        assert "GF_AUTH_GENERIC_OAUTH_CLIENT_ID" in env

    def test_compose_has_postgres_database_config(self):
        """Grafana's compose block includes the postgres DB config."""
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        compose = grafana.compose_service(cfg=None)
        env = compose.get("environment", {})
        assert "GF_DATABASE_TYPE" in env
        assert env["GF_DATABASE_TYPE"] == "postgres"


# ── Phase B: secrets_needed() + credentials() ────────────────────────────────


class TestGrafanaSecretsNeeded:
    """plugin.secrets_needed() returns the SecretSpec list this service requires.

    Replaces the hardcoded SecretSpec list in secrets.py. The default
    implementation reads `required_secrets` from service.yaml.
    """

    def test_returns_list_of_secret_specs(self):
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        specs = grafana.secrets_needed()
        # Grafana needs at minimum: admin password, OIDC secret, DB password
        spec_names = {s.name for s in specs}
        assert "GRAFANA_ADMIN_PASSWORD" in spec_names
        assert "GRAFANA_OIDC_SECRET" in spec_names
        assert "GRAFANA_DB_PASSWORD" in spec_names

    def test_specs_have_tier(self):
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        specs = grafana.secrets_needed()
        # SecretTier is a StrEnum: USER="user", GENERATED="gen", DERIVED="derived"
        valid_tiers = {"user", "gen", "derived"}
        for spec in specs:
            assert str(spec.tier) in valid_tiers, f"{spec.name} has tier {spec.tier!r}"


class TestGrafanaCredentials:
    """plugin.credentials(cfg) returns Vaultwarden credential entries.

    Replaces the hardcoded credential_entries in credential_catalog.py.
    Default: derive from routes subdomains + service name.
    """

    def test_returns_admin_credential_entry(self):
        from toolkit.services import discover_service_plugins

        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        creds = grafana.credentials(cfg=None)
        # At minimum, a credential for the Grafana admin login
        assert len(creds) >= 1
        admin_cred = [c for c in creds if "grafana" in c.name.lower() or "admin" in c.name.lower()]
        assert admin_cred, f"No admin credential in {[c.name for c in creds]}"

    def test_credential_url_uses_subdomain(self):
        from toolkit.core.config.config import Config
        from toolkit.services import discover_service_plugins

        cfg = Config(domain="example.com")
        plugins = {p.service: p for p in discover_service_plugins()}
        grafana = plugins["grafana"]
        creds = grafana.credentials(cfg=cfg)
        # CredentialEntry has url_template, not url
        urls = [c.url_template for c in creds if c.url_template]
        assert any("grafana" in u for u in urls), f"No grafana URL in {urls}"


# ── Phase C: every declared service discovered + valid ────────────────────────


class TestAllServicesDiscovered:
    """Every service listed in category.yaml files should be discoverable in services/."""

    def test_discovers_every_declared_service(self):
        """Every service manifest must have a discoverable plugin."""
        from toolkit.services import _SERVICES_DIR, _reset_cache, discover_service_plugins

        _reset_cache()
        discovered = {plugin.service for plugin in discover_service_plugins()}
        declared = {
            yaml.safe_load(path.read_text(encoding="utf-8"))["name"] for path in _SERVICES_DIR.glob("*/service.yaml")
        }
        assert discovered == declared

    def test_every_service_has_identity(self):
        """Every discovered service must have name, category, and node set."""
        from toolkit.services import _reset_cache, discover_service_plugins

        _reset_cache()
        for plugin in discover_service_plugins():
            assert plugin.service, f"Service name empty on {plugin}"
            assert plugin.category, f"Category empty on {plugin.service}"
            assert plugin.placement, f"Placement empty on {plugin.service}"

    def test_every_service_has_compose_yaml(self):
        """Every container runtime owns a standalone Compose application."""

        from toolkit.core.manifest.schema import ServiceManifest
        from toolkit.services import _SERVICES_DIR, _reset_cache, discover_service_plugins

        _reset_cache()
        missing: list[str] = []
        for plugin in discover_service_plugins():
            manifest = ServiceManifest.model_validate(plugin._yaml_data)
            compose_path = _SERVICES_DIR / plugin.service / "compose.yaml"
            if manifest.runtime == "container" and not compose_path.is_file():
                missing.append(plugin.service)
            if manifest.runtime == "embedded":
                assert not compose_path.exists(), plugin.service
                continue
            compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
            assert isinstance(compose, dict) and isinstance(compose.get("services"), dict), plugin.service
            assert plugin.service in compose["services"], plugin.service
        assert not missing, f"Missing compose.yaml for: {missing}"

    def test_every_service_yaml_has_required_fields(self):
        """Every service manifest must have its required identity fields."""
        import yaml
        from toolkit.services import _SERVICES_DIR, _reset_cache, discover_service_plugins

        _reset_cache()
        for plugin in discover_service_plugins():
            yaml_path = _SERVICES_DIR / plugin.service / "service.yaml"
            data = yaml.safe_load(yaml_path.read_text())
            assert "name" in data, f"{plugin.service}: missing 'name' in service.yaml"
            assert "category" in data, f"{plugin.service}: missing 'category'"
            assert "placement" in data, f"{plugin.service}: missing 'placement'"

    def test_no_duplicate_service_names(self):
        """No two service directories should declare the same name."""
        from toolkit.services import _reset_cache, discover_service_plugins

        _reset_cache()
        names = [p.service for p in discover_service_plugins()]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"Duplicate service names: {duplicates}"


def _manifest_data(**overrides) -> dict:
    return {
        "name": "test",
        "label": "Test",
        "description": "Test service",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
        **overrides,
    }


class TestServiceManifestSchema:
    """The schema catches typos like restart_policy: carful at load time."""

    def test_valid_service_yaml_passes_validation(self):
        from toolkit.core.manifest.schema import ServiceManifest

        model = ServiceManifest.model_validate(_manifest_data(restart_policy="careful", memory_tier="medium"))
        assert model.restart_policy == "careful"
        assert model.memory_tier == "medium"

    def test_bad_restart_policy_fails(self):
        """Typo 'carful' (instead of 'careful') must fail validation."""
        from pydantic import ValidationError
        from toolkit.core.manifest.schema import ServiceManifest

        with pytest.raises(ValidationError, match="restart_policy"):
            ServiceManifest.model_validate(_manifest_data(restart_policy="carful"))

    def test_bad_memory_tier_fails(self):
        from pydantic import ValidationError
        from toolkit.core.manifest.schema import ServiceManifest

        with pytest.raises(ValidationError, match="memory_tier"):
            ServiceManifest.model_validate(_manifest_data(memory_tier="invalid"))

    def test_bad_placement_fails(self):
        from pydantic import ValidationError
        from toolkit.core.manifest.schema import ServiceManifest

        with pytest.raises(ValidationError, match="placement"):
            ServiceManifest.model_validate(_manifest_data(placement="INVALID_NODE"))

    def test_valid_restart_policies_accepted(self):
        from toolkit.core.manifest.schema import ServiceManifest

        for policy in ("safe", "careful", "never"):
            model = ServiceManifest.model_validate(_manifest_data(restart_policy=policy))
            assert model.restart_policy == policy

    def test_key_services_present(self):
        """Critical services must be in the services/ directory."""
        from toolkit.services import _reset_cache, discover_service_plugins

        _reset_cache()
        services = {p.service for p in discover_service_plugins()}
        critical = {
            "authelia",
            "postgres",
            "redis",
            "caddy",
            "prometheus",
            "grafana",
            "loki",
            "nextcloud",
            "immich-server",
            "vaultwarden",
            "headscale",
            "ntfy",
            "sonarr",
            "radarr",
            "prowlarr",
            "mailserver",
            "adguard",
            "komodo-core",
        }
        missing = critical - services
        assert not missing, f"Critical services missing from services/: {missing}"
