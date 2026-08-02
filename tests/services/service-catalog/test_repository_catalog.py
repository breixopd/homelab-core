from __future__ import annotations

import re
from pathlib import Path

import yaml
from toolkit.core.manifest.catalog import clear_catalog_cache, load_service_catalog

_SERVICES = Path(__file__).resolve().parents[3] / "toolkit" / "services"


def test_every_repository_service_uses_the_strict_manifest_contract() -> None:
    clear_catalog_cache()
    catalog = load_service_catalog()

    manifest_paths = tuple(_SERVICES.glob("*/service.yaml"))

    assert len(catalog.manifests) == len(manifest_paths)
    assert len(catalog.names) == len(set(catalog.names))


def test_music_sync_declares_runtime_writable_storage_ownership() -> None:
    catalog = load_service_catalog()
    music_sync = catalog.require("music-sync")
    config = next(asset for asset in music_sync.data_specs if asset.name == "music-sync-config")
    media_layout = catalog.require("media-library").host_paths[0]

    assert (config.host_uid, config.host_gid) == (1000, 1000)
    assert "music" in media_layout.subdirs


def test_postgres_manifests_match_their_container_runtime_uid() -> None:
    catalog = load_service_catalog()

    for name in ("postgres", "immich-postgres"):
        manifest = catalog.require(name)
        database = manifest.data_specs[0]
        assert (database.host_uid, database.host_gid) == (999, 999)


def test_wazuh_certificates_match_the_indexer_runtime_uid() -> None:
    catalog = load_service_catalog()
    certificates = next(
        asset for asset in catalog.require("wazuh-indexer").data_specs if asset.name == "wazuh-certificates"
    )

    assert (certificates.host_uid, certificates.host_gid) == (1000, 1000)


def test_redis_data_matches_the_runtime_user() -> None:
    catalog = load_service_catalog()
    redis = next(asset for asset in catalog.require("redis").data_specs if asset.name == "redis-data")

    assert (redis.host_uid, redis.host_gid) == (999, 1000)


def test_dashboard_metrics_services_are_selected_by_capability() -> None:
    catalog = load_service_catalog()
    collector = catalog.provider("metrics")
    dashboard = catalog.provider("metrics-dashboard")

    assert collector is not None
    assert dashboard is not None
    assert collector.name != dashboard.name


def test_framework_schema_does_not_name_plugin_redirect_protocols() -> None:
    schema = (_SERVICES.parent / "core/manifest/schema.py").read_text(encoding="utf-8")

    assert "app.immich" not in schema


def test_manifest_variables_do_not_name_the_ingress_implementation() -> None:
    compiler = (_SERVICES.parent / "core/manifest/variables.py").read_text(encoding="utf-8")
    navidrome = (_SERVICES / "navidrome/service.yaml").read_text(encoding="utf-8")

    assert "'caddy'" not in compiler
    assert '"caddy"' not in compiler
    assert "{derived.edge_proxy_cidr}" in navidrome


def test_tdarr_automation_is_owned_by_its_service_plugin() -> None:
    toolkit = _SERVICES.parent

    assert (toolkit / "services/tdarr/bootstrap.py").is_file()
    assert not (toolkit / "core/ops/tdarr_automation.py").exists()


def test_generic_capability_detector_contains_no_service_policy() -> None:
    detector = (_SERVICES.parent / "core/capabilities/detect.py").read_text(encoding="utf-8").lower()

    assert "tdarr" not in detector
    assert "jellyfin" not in detector


def test_ansible_inventory_is_generated_from_machine_plugins() -> None:
    root = _SERVICES.parents[1]
    validator = (root / "scripts/validate-fresh-deploy.sh").read_text(encoding="utf-8")

    assert not (root / "automation/ansible/inventory/hosts.example.yml").exists()
    assert "inventory/hosts.example.yml" not in validator
    assert "inventory/hosts.yml" in validator


def test_database_runtime_is_owned_by_the_service_sdk() -> None:
    toolkit = _SERVICES.parent

    assert (toolkit / "services/sdk/postgres.py").is_file()
    assert not (toolkit / "core/bootstrap/postgres_bootstrap.py").exists()
    assert not (toolkit / "core/bootstrap/projects_db.py").exists()


def test_ntfy_transport_is_owned_by_its_service_plugin() -> None:
    toolkit = _SERVICES.parent

    assert (toolkit / "services/ntfy/client.py").is_file()
    assert not (toolkit / "core/ops/ntfy.py").exists()


def test_caddy_renderer_is_owned_by_its_service_plugin() -> None:
    toolkit = _SERVICES.parent

    assert (toolkit / "services/caddy/routes.py").is_file()
    assert not (toolkit / "core/manifest/caddy.py").exists()


def test_registry_mirror_bootstrap_is_owned_by_its_service_plugin() -> None:
    toolkit = _SERVICES.parent

    assert (toolkit / "services/registry-mirror/bootstrap.py").is_file()
    assert not (toolkit / "core/compose/registry_mirror.py").exists()


def test_repository_contains_no_implicit_or_noncanonical_route_policy() -> None:
    for path in sorted(_SERVICES.glob("*/service.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "service" not in {key for route in raw.get("routes", []) for key in route}, path
        for route in raw.get("routes", []):
            assert route.get("exposure") in {"public", "private"}, path
            assert isinstance(route.get("auth"), dict), path
            assert route["auth"].get("mode") in {"forward_auth", "oidc", "native", "split", "none"}, path


def test_every_sensitive_compose_reference_has_a_declared_owner() -> None:
    from toolkit.core.secrets.secrets import INFRASTRUCTURE_SECRETS

    catalog = load_service_catalog()
    owned = {secret.name for manifest in catalog.manifests for secret in manifest.required_secrets}
    owned.update(secret.name for secret in INFRASTRUCTURE_SECRETS)
    owned.update(projection.target_env for manifest in catalog.manifests for projection in manifest.secret_projections)
    projected_sources = {
        projection.source_env for manifest in catalog.manifests for projection in manifest.secret_projections
    }
    assert projected_sources - owned == set()
    pattern = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
    sensitive_tokens = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CLAIM", "CREDENTIAL")
    referenced = {
        name
        for path in _SERVICES.glob("*/compose.yaml")
        for name in pattern.findall(path.read_text(encoding="utf-8"))
        if any(token in name for token in sensitive_tokens)
    }

    assert referenced - owned == set()


def test_every_compose_dependency_is_declared_by_its_plugin() -> None:
    catalog = load_service_catalog()
    runtime_owners: dict[str, str] = {}
    compose_documents: dict[str, dict] = {}
    for manifest in catalog.manifests:
        path = _SERVICES / manifest.name / "compose.yaml"
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        compose_documents[manifest.name] = document
        for runtime in document.get("services") or {}:
            assert runtime not in runtime_owners, f"runtime {runtime!r} has more than one plugin owner"
            runtime_owners[runtime] = manifest.name

    undeclared: list[str] = []
    for manifest in catalog.manifests:
        services = compose_documents.get(manifest.name, {}).get("services") or {}
        for runtime, spec in services.items():
            dependencies = spec.get("depends_on") or {}
            names = dependencies if isinstance(dependencies, list) else dependencies.keys()
            for dependency in names:
                owner = runtime_owners.get(dependency)
                if owner is None:
                    undeclared.append(f"{manifest.name}:{runtime} references unowned runtime {dependency}")
                elif owner != manifest.name and owner not in manifest.depends_on:
                    undeclared.append(f"{manifest.name}:{runtime} omits dependency plugin {owner}")

    assert undeclared == []
