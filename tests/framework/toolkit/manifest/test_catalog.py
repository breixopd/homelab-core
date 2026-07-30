from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from toolkit.core.manifest.catalog import ManifestCatalogError, clear_catalog_cache, load_service_catalog


def _write_manifest(
    root: Path,
    name: str,
    *,
    depends_on: list[str] | None = None,
    integrations: list[dict[str, object]] | None = None,
    subdomain: str | None = None,
    provides: list[str] | None = None,
    internal_aliases: list[str] | None = None,
) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    source_env = f"{name.upper().replace('-', '_')}_DATA_SOURCE"
    route = {
        "upstream": f"{name}:8080",
        "exposure": "private",
        "auth": {"mode": "forward_auth"},
    }
    if subdomain is not None:
        route["subdomain"] = subdomain
    data = {
        "name": name,
        "label": name.title(),
        "description": f"{name} service",
        "icon": "box",
        "category": "cloud",
        "placement": "apps",
        "priority": 50,
        "depends_on": depends_on or [],
        "integrations": integrations or [],
        "provides": provides or [],
        "internal_aliases": internal_aliases or [],
        "host_sources": {source_env: {"path": f"data/{name}"}},
        "routes": [route],
    }
    (directory / "service.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    compose = {
        "services": {
            name: {
                "image": "example:1.0@sha256:" + ("a" * 64),
                "volumes": [f"${{{source_env}:-./data/{name}}}:/var/lib/example:ro"],
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            }
        }
    }
    (directory / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_catalog_cache()


def test_catalog_loads_without_importing_plugin_code(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    (tmp_path / "example" / "plugin.py").write_text("raise RuntimeError('must not import')\n", encoding="utf-8")

    catalog = load_service_catalog(tmp_path)

    assert catalog.names == ("example",)
    assert catalog.require("example").label == "Example"


def test_catalog_resolves_framework_providers_by_declared_capability(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "proxy", provides=["ingress"])
    _write_manifest(tmp_path, "metrics", provides=["metrics"])

    catalog = load_service_catalog(tmp_path)

    assert catalog.provider("ingress").name == "proxy"
    assert catalog.provider("metrics").name == "metrics"
    assert catalog.provider("directory") is None


def test_catalog_rejects_duplicate_framework_capability_providers(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "proxy-a", provides=["ingress"])
    _write_manifest(tmp_path, "proxy-b", provides=["ingress"])

    with pytest.raises(ManifestCatalogError, match="capability 'ingress'.*proxy-a.*proxy-b"):
        load_service_catalog(tmp_path)


def test_catalog_requires_bounded_container_logging(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"].pop("logging")
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="bounded logging"):
        load_service_catalog(tmp_path)


def test_catalog_requires_runtime_compose_profile_on_declared_service(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runtimes"] = {"example-agent": {"placements": ["@non-primary"], "compose_profile": "agents"}}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="places unknown runtime service 'example-agent'"):
        load_service_catalog(tmp_path)

    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example-agent"] = {
        "image": "example-agent:1.0@sha256:" + ("b" * 64),
        "profiles": ["wrong"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    with pytest.raises(ManifestCatalogError, match="requires Compose profile 'agents'"):
        load_service_catalog(tmp_path)

    compose["services"]["example-agent"]["profiles"] = ["agents"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").runtimes["example-agent"].compose_profile == "agents"


def test_catalog_requires_manifest_contract_for_compose_build(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["build"] = {"context": "./example/image"}
    compose["services"]["example"]["image"] = "${HOMELAB_EXAMPLE_IMAGE:?generate first}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="build without image_build"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_unpinned_runtime_image(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["image"] = "example:1.0"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="immutable image reference"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_image_less_runtime(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"].pop("image")
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="must declare an image or build"):
        load_service_catalog(tmp_path)


def test_catalog_validates_image_contract_against_compose(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["image_build"] = {"context": "image", "env_var": "HOMELAB_EXAMPLE_IMAGE"}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (tmp_path / "example" / "image").mkdir()
    (tmp_path / "example" / "image" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["build"] = {"context": "./wrong/image"}
    compose["services"]["example"]["image"] = "${WRONG_IMAGE:?generate first}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="context"):
        load_service_catalog(tmp_path)

    compose["services"]["example"]["build"]["context"] = "./example/image"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ManifestCatalogError, match="HOMELAB_EXAMPLE_IMAGE"):
        load_service_catalog(tmp_path)

    compose["services"]["example"]["image"] = "${HOMELAB_EXAMPLE_IMAGE_OTHER:?generate first}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ManifestCatalogError, match="HOMELAB_EXAMPLE_IMAGE"):
        load_service_catalog(tmp_path)

    compose["services"]["example"]["image"] = "${HOMELAB_EXAMPLE_IMAGE:?generate first}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").image_build.context == "image"


def test_catalog_validates_release_image_digest_against_compose(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["image_release"] = {
        "compose_service": "example",
        "repository": "ghcr.io/example/example",
        "version": "v1.2.3",
        "digest": "sha256:" + ("a" * 64),
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="immutable image"):
        load_service_catalog(tmp_path)

    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["image"] = "ghcr.io/example/example:v1.2.3@sha256:" + ("a" * 64)
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").image_release.version == "v1.2.3"


def test_catalog_rejects_duplicate_image_environment_keys(tmp_path: Path) -> None:
    for name in ("alpha", "bravo"):
        _write_manifest(tmp_path, name)
        manifest_path = tmp_path / name / "service.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["image_build"] = {"context": "image", "env_var": "HOMELAB_SHARED_IMAGE"}
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        (tmp_path / name / "image").mkdir()
        (tmp_path / name / "image" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        compose_path = tmp_path / name / "compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        compose["services"][name]["build"] = {"context": f"./{name}/image"}
        compose["services"][name]["image"] = "${HOMELAB_SHARED_IMAGE:?generate first}"
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="image environment.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_image_repository_ownership(tmp_path: Path) -> None:
    for name in ("alpha", "bravo"):
        _write_manifest(tmp_path, name)
        manifest_path = tmp_path / name / "service.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["image_build"] = {
            "context": "image",
            "env_var": f"HOMELAB_{name.upper()}_IMAGE",
            "repository": "shared-image",
        }
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        (tmp_path / name / "image").mkdir()
        (tmp_path / name / "image" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        compose_path = tmp_path / name / "compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        compose["services"][name]["build"] = {"context": f"./{name}/image"}
        compose["services"][name]["image"] = f"${{HOMELAB_{name.upper()}_IMAGE:-example:latest}}"
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="image repository.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def _declare_database_provider(root: Path, name: str) -> None:
    path = root / name / "service.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    prefix = name.upper().replace("-", "_")
    manifest["variables"] = {
        f"{prefix}_USER": "admin",
        f"{prefix}_DB": "postgres",
    }
    manifest["required_secrets"] = [
        {
            "name": f"{prefix}_PASSWORD",
            "tier": "generated",
            "description": "database administrator password",
            "rotation": "reconcile",
        }
    ]
    manifest["database_provider"] = {
        "engine": "postgresql",
        "admin_username_env": f"{prefix}_USER",
        "admin_password_env": f"{prefix}_PASSWORD",
        "admin_database_env": f"{prefix}_DB",
    }
    manifest["service_endpoint"] = {"container_port": 5432, "published_port": 5432}
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    compose_path = root / name / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"][name]["environment"] = {
        "POSTGRES_USER": f"${{{prefix}_USER}}",
        "POSTGRES_PASSWORD": f"${{{prefix}_PASSWORD}}",
        "POSTGRES_DB": f"${{{prefix}_DB}}",
    }
    compose["services"][name]["ports"] = ["${PRIVATE_IP}:5432:5432"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")


def _declare_database_binding(root: Path, name: str, provider: str, database: str = "application") -> None:
    path = root / name / "service.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["depends_on"] = [provider]
    manifest["required_secrets"] = [
        {
            "name": "APP_DB_PASSWORD",
            "tier": "generated",
            "description": "application database password",
            "rotation": "reconcile",
        }
    ]
    manifest["databases"] = [
        {
            "provider": provider,
            "database": database,
            "username": database,
            "host_env": "APP_DB_HOST",
            "port_env": "APP_DB_PORT",
            "database_env": "APP_DB_NAME",
            "username_env": "APP_DB_USER",
            "password_env": "APP_DB_PASSWORD",
        }
    ]
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def test_catalog_validates_database_provider_and_consumer_contracts(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _write_manifest(tmp_path, "application")
    _declare_database_provider(tmp_path, "database")
    _declare_database_binding(tmp_path, "application", "database")

    catalog = load_service_catalog(tmp_path)

    assert catalog.require("database").database_provider is not None
    assert catalog.require("application").databases[0].provider == "database"


def test_catalog_rejects_database_binding_without_provider_dependency(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _write_manifest(tmp_path, "application")
    _declare_database_provider(tmp_path, "database")
    _declare_database_binding(tmp_path, "application", "database")
    path = tmp_path / "application" / "service.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["depends_on"] = []
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="database provider.*dependency"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_binding_to_service_without_database_provider(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _write_manifest(tmp_path, "application")
    _declare_database_binding(tmp_path, "application", "database")

    with pytest.raises(ManifestCatalogError, match="does not declare a database provider"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_database_or_role_ownership(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _write_manifest(tmp_path, "application")
    _write_manifest(tmp_path, "worker")
    _declare_database_provider(tmp_path, "database")
    _declare_database_binding(tmp_path, "application", "database")
    _declare_database_binding(tmp_path, "worker", "database")

    with pytest.raises(ManifestCatalogError, match="database.*owned by both.*application.*worker"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_database_provider_runtime_mismatch(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _declare_database_provider(tmp_path, "database")
    path = tmp_path / "database" / "service.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["service_endpoint"]["compose_service"] = "missing"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="endpoint runtime.*missing"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_database_provider_environment_mismatch(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _declare_database_provider(tmp_path, "database")
    compose_path = tmp_path / "database" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["database"]["environment"].pop("POSTGRES_DB")
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="DATABASE_DB.*not referenced"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_database_provider_publication_mismatch(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _declare_database_provider(tmp_path, "database")
    compose_path = tmp_path / "database" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["database"]["ports"] = ["${PRIVATE_IP}:5544:5432"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="published port 5432"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_missing_image_dockerfile(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["image_build"] = {"context": "image", "env_var": "HOMELAB_EXAMPLE_IMAGE"}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (tmp_path / "example" / "image").mkdir()
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["build"] = {"context": "./example/image"}
    compose["services"]["example"]["image"] = "${HOMELAB_EXAMPLE_IMAGE:?generate first}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="Dockerfile does not exist"):
        load_service_catalog(tmp_path)


def test_catalog_requires_directory_name_to_equal_manifest_name(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["name"] = "other"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="directory"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_compose_bind_without_host_source_owner(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("host_sources")
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="EXAMPLE_DATA_SOURCE.*no manifest owner"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_host_source_owners(tmp_path: Path) -> None:
    for name in ("alpha", "bravo"):
        _write_manifest(tmp_path, name)
        path = tmp_path / name / "service.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["host_sources"] = {"SHARED_DATA_SOURCE": {"path": f"data/{name}"}}
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        compose_path = tmp_path / name / "compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        compose["services"][name]["volumes"] = ["${SHARED_DATA_SOURCE:-./data/shared}:/var/lib/example:ro"]
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="host source.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_cross_service_config_path_overlap(tmp_path: Path) -> None:
    for name, source_path in (("alpha", "config/shared"), ("bravo", "config/shared/secret")):
        _write_manifest(tmp_path, name)
        manifest_path = tmp_path / name / "service.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        source_name = next(iter(manifest["host_sources"]))
        manifest["host_sources"][source_name]["path"] = source_path
        manifest["host_sources"][source_name]["static"] = True
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        compose_path = tmp_path / name / "compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        compose["services"][name]["volumes"] = [f"${{{source_name}:-./{source_path}}}:/var/lib/example:ro"]
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="config host source path.*overlaps"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_writable_runtime_state_under_static_config(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    source_name = next(iter(manifest["host_sources"]))
    manifest["host_sources"][source_name] = {"path": "config/example", "static": True}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"] = [f"${{{source_name}:-./config/example}}:/var/lib/example"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="undeclared writable Compose mount|static host source.*read-only"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_internal_dns_aliases(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha", internal_aliases=["shared"])
    _write_manifest(tmp_path, "bravo", internal_aliases=["shared"])

    with pytest.raises(ManifestCatalogError, match="internal DNS alias 'shared'.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_unused_host_source_contract(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["host_sources"]["UNUSED_DATA_SOURCE"] = {"path": "data/unused"}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="UNUSED_DATA_SOURCE.*not used"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_cross_placement_host_source_consumers(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha")
    _write_manifest(tmp_path, "bravo")
    alpha_path = tmp_path / "alpha" / "service.yaml"
    alpha = yaml.safe_load(alpha_path.read_text(encoding="utf-8"))
    alpha["host_sources"] = {"SHARED_DATA_SOURCE": {"path": "data/shared"}}
    alpha_path.write_text(yaml.safe_dump(alpha, sort_keys=False), encoding="utf-8")
    alpha_compose_path = tmp_path / "alpha" / "compose.yaml"
    alpha_compose = yaml.safe_load(alpha_compose_path.read_text(encoding="utf-8"))
    alpha_compose["services"]["alpha"]["volumes"] = ["${SHARED_DATA_SOURCE:-./data/shared}:/var/lib/example:ro"]
    alpha_compose_path.write_text(yaml.safe_dump(alpha_compose, sort_keys=False), encoding="utf-8")

    bravo_path = tmp_path / "bravo" / "service.yaml"
    bravo = yaml.safe_load(bravo_path.read_text(encoding="utf-8"))
    bravo["placement"] = "media"
    bravo.pop("host_sources")
    bravo_path.write_text(yaml.safe_dump(bravo, sort_keys=False), encoding="utf-8")
    bravo_compose_path = tmp_path / "bravo" / "compose.yaml"
    bravo_compose = yaml.safe_load(bravo_compose_path.read_text(encoding="utf-8"))
    bravo_compose["services"]["bravo"]["volumes"] = ["${SHARED_DATA_SOURCE:-./data/shared}:/var/lib/example:ro"]
    bravo_compose_path.write_text(yaml.safe_dump(bravo_compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="SHARED_DATA_SOURCE.*alpha.*bravo.*placement"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_host_source_fallback_that_differs_from_owner_path(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"] = ["${EXAMPLE_DATA_SOURCE:-./data/other}:/var/lib/example:ro"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="EXAMPLE_DATA_SOURCE.*fallback.*data/example"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_generated_artifact_owners(tmp_path: Path) -> None:
    for name in ("alpha", "bravo"):
        _write_manifest(tmp_path, name)
        path = tmp_path / name / "service.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["generated_artifacts"] = [{"path": "generated/shared.conf"}]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="generated artifact.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def test_catalog_requires_artifact_for_generated_host_source(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["host_sources"]["EXAMPLE_DATA_SOURCE"]["path"] = "generated/example.conf"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"] = ["${EXAMPLE_DATA_SOURCE:-./generated/example.conf}:/var/lib/example:ro"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="generated host source.*EXAMPLE_DATA_SOURCE.*artifact"):
        load_service_catalog(tmp_path)


def test_catalog_accepts_generated_host_source_directory_with_owned_artifact(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["host_sources"]["EXAMPLE_DATA_SOURCE"]["path"] = "generated/example"
    manifest["generated_artifacts"] = [{"path": "generated/example/config.yml"}]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"] = ["${EXAMPLE_DATA_SOURCE:-./generated/example}:/var/lib/example:ro"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    assert load_service_catalog(tmp_path).require("example").generated_artifacts[0].path == (
        "generated/example/config.yml"
    )


def test_catalog_requires_artifact_for_install_root_generated_mount(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"].append("${INSTALL_ROOT:-.}/generated/example.conf:/etc/example.conf:ro")
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="generated Compose source.*generated/example.conf.*artifact"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_cross_placement_generated_artifact_mount(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha")
    _write_manifest(tmp_path, "bravo")
    alpha_path = tmp_path / "alpha" / "service.yaml"
    alpha = yaml.safe_load(alpha_path.read_text(encoding="utf-8"))
    alpha["generated_artifacts"] = [{"path": "generated/shared.conf"}]
    alpha_path.write_text(yaml.safe_dump(alpha, sort_keys=False), encoding="utf-8")
    bravo_path = tmp_path / "bravo" / "service.yaml"
    bravo = yaml.safe_load(bravo_path.read_text(encoding="utf-8"))
    bravo["placement"] = "core"
    bravo_path.write_text(yaml.safe_dump(bravo, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "bravo" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["bravo"]["volumes"].append("${INSTALL_ROOT:-.}/generated/shared.conf:/etc/shared.conf:ro")
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="generated artifact.*alpha.*bravo.*placement"):
        load_service_catalog(tmp_path)


def test_catalog_requires_runtime_scope_for_artifact_on_alternate_runtime(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtimes"] = {"example-agent": {"placements": ["@non-primary"], "compose_profile": "agents"}}
    manifest["generated_artifacts"] = [{"path": "generated/example-agent.conf"}]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example-agent"] = {
        "image": "example-agent:1.0@sha256:" + ("b" * 64),
        "profiles": ["agents"],
        "volumes": ["${INSTALL_ROOT:-.}/generated/example-agent.conf:/etc/example.conf:ro"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="must declare runtime_service"):
        load_service_catalog(tmp_path)

    manifest["generated_artifacts"][0]["runtime_service"] = "example-agent"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").generated_artifacts[0].runtime_service == "example-agent"


def test_catalog_rejects_missing_dependencies(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example", depends_on=["postgres"])

    with pytest.raises(ManifestCatalogError, match="unknown service 'postgres'"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_unknown_network_listener_references(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["network_listeners"] = [{"id": "api", "port": 8080, "sources": ["@service:missing"]}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["ports"] = ["${PRIVATE_IP:-127.0.0.1}:8080:8080"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="unknown service 'missing'"):
        load_service_catalog(tmp_path)


def test_catalog_requires_network_listener_to_publish_its_host_port(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["network_listeners"] = [{"id": "api", "port": 9090, "sources": ["@all"]}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="non-loopback tcp publication"):
        load_service_catalog(tmp_path)

    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["ports"] = ["${PRIVATE_IP:-127.0.0.1}:9090:9090"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").network_listeners[0].port == 9090


def test_catalog_rejects_dependency_cycles_with_the_cycle_path(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha", depends_on=["bravo"])
    _write_manifest(tmp_path, "bravo", depends_on=["charlie"])
    _write_manifest(tmp_path, "charlie", depends_on=["alpha"])

    with pytest.raises(ManifestCatalogError, match=r"alpha -> bravo -> charlie -> alpha"):
        load_service_catalog(tmp_path)


def test_catalog_requires_reachable_cross_node_service_endpoint(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "database")
    _write_manifest(
        tmp_path,
        "example",
        depends_on=["database"],
        integrations=[{"service": "database", "host_env": "EXAMPLE_DB_HOST", "port_env": "EXAMPLE_DB_PORT"}],
    )
    database_path = tmp_path / "database" / "service.yaml"
    database = yaml.safe_load(database_path.read_text(encoding="utf-8"))
    database["placement"] = "control"
    database_path.write_text(yaml.safe_dump(database, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="requires a service endpoint"):
        load_service_catalog(tmp_path)

    database["service_endpoint"] = {"container_port": 5432, "published_port": 5433}
    database_path.write_text(yaml.safe_dump(database, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ManifestCatalogError, match="non-loopback published port"):
        load_service_catalog(tmp_path)

    compose_path = tmp_path / "database" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["database"]["ports"] = ["${PRIVATE_IP:-127.0.0.1}:5433:5432"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    endpoint = load_service_catalog(tmp_path).require("database").service_endpoint
    assert endpoint is not None
    assert endpoint.published_port == 5433


def test_catalog_rejects_services_owned_by_unknown_categories(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["category"] = "photos"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="unknown category 'photos'"):
        load_service_catalog(tmp_path)


def test_catalog_requires_host_port_for_cross_node_prometheus_scrapes(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "prometheus", provides=["metrics"])
    _write_manifest(tmp_path, "example")
    prometheus_path = tmp_path / "prometheus" / "service.yaml"
    prometheus = yaml.safe_load(prometheus_path.read_text(encoding="utf-8"))
    prometheus["placement"] = "monitoring"
    prometheus_path.write_text(yaml.safe_dump(prometheus, sort_keys=False), encoding="utf-8")
    example_path = tmp_path / "example" / "service.yaml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    example["prometheus"] = [{"id": "server", "container_port": 9100}]
    example_path.write_text(yaml.safe_dump(example, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="host_port"):
        load_service_catalog(tmp_path)

    example["prometheus"][0]["host_port"] = 19100
    example_path.write_text(yaml.safe_dump(example, sort_keys=False), encoding="utf-8")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["ports"] = ["${PRIVATE_IP:-127.0.0.1}:19100:9100"]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").prometheus[0].host_port == 19100


def test_catalog_rejects_unknown_prometheus_host_integration(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "prometheus", provides=["metrics"])
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["prometheus"] = [{"id": "external", "host_port": 9100, "host_integration": "missing-agent"}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="unknown host integration 'missing-agent'"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_conflicting_prometheus_job_paths(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "prometheus", provides=["metrics"])
    _write_manifest(tmp_path, "alpha")
    _write_manifest(tmp_path, "bravo")
    for name, path_value, host_port in (("alpha", "/metrics", 19100), ("bravo", "/custom", 19101)):
        path = tmp_path / name / "service.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["prometheus"] = [
            {
                "id": "server",
                "job": "shared",
                "container_port": 9100,
                "host_port": host_port,
                "path": path_value,
            }
        ]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        compose_path = tmp_path / name / "compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        compose["services"][name]["ports"] = [f"${{PRIVATE_IP:-127.0.0.1}}:{host_port}:9100"]
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="conflicting paths"):
        load_service_catalog(tmp_path)


def test_catalog_requires_published_ports_for_cross_node_routes(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "caddy", provides=["ingress"])
    _write_manifest(tmp_path, "example")
    caddy_path = tmp_path / "caddy" / "service.yaml"
    caddy = yaml.safe_load(caddy_path.read_text(encoding="utf-8"))
    caddy["placement"] = "control"
    caddy_path.write_text(yaml.safe_dump(caddy, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="published_port"):
        load_service_catalog(tmp_path)

    example_path = tmp_path / "example" / "service.yaml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    example["routes"][0]["published_port"] = 8080
    example_path.write_text(yaml.safe_dump(example, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").routes[0].published_port == 8080


def test_catalog_rejects_unknown_manifest_variable_config_path(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["variables"] = {"EXAMPLE_VALUE": "{config.runtime.does_not_exist}"}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="does_not_exist"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_predicates_for_unknown_service_settings(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["enabled_when"] = [{"setting": "missing.enabled", "equals": True}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="unknown service setting 'missing.enabled'"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_predicate_values_incompatible_with_setting(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["management"] = {"settings": [{"key": "enabled", "label": "Enabled", "type": "boolean", "default": True}]}
    data["enabled_when"] = [{"setting": "example.enabled", "equals": "yes"}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="incompatible predicate value"):
        load_service_catalog(tmp_path)


def test_catalog_requires_setup_secret_conditions_to_use_setup_settings(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["management"] = {"settings": [{"key": "enabled", "label": "Enabled", "type": "boolean", "default": True}]}
    data["required_secrets"] = [
        {
            "name": "EXAMPLE_TOKEN",
            "tier": "user",
            "description": "Example API token",
            "setup": {
                "label": "API token",
                "required": True,
                "when": [{"setting": "example.enabled", "equals": True}],
            },
        }
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="not exposed in setup"):
        load_service_catalog(tmp_path)

    data["management"]["settings"][0]["setup"] = True
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert load_service_catalog(tmp_path).require("example").required_secrets[0].setup is not None


def test_catalog_rejects_credential_referencing_undeclared_secret(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["credentials"] = [
        {
            "name": "Example Admin",
            "url": "https://example.{domain}",
            "password_env": "UNDECLARED_PASSWORD",
        }
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="undeclared password environment"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_credential_names(tmp_path: Path) -> None:
    for service in ("alpha", "bravo"):
        _write_manifest(tmp_path, service)
        path = tmp_path / service / "service.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        secret = f"{service.upper()}_PASSWORD"
        data["required_secrets"] = [
            {"name": secret, "tier": "generated", "description": f"{service} password", "rotation": "persistent"}
        ]
        data["credentials"] = [
            {
                "name": "Shared Admin",
                "url": f"https://{service}.{{domain}}",
                "password_env": secret,
            }
        ]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="credential name 'Shared Admin'.*alpha.*bravo"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_storage_asset_without_matching_compose_mount(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"][0] = "${EXAMPLE_DATA_SOURCE:-./data/example}:/var/lib/example"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["stateful"] = True
    data["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "OTHER_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
        }
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="matching Compose mount"):
        load_service_catalog(tmp_path)


def test_catalog_accepts_storage_asset_matching_compose_mount(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"][0] = "${EXAMPLE_DATA_SOURCE:-./data/example}:/var/lib/example"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["stateful"] = True
    data["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
        }
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    assert load_service_catalog(tmp_path).require("example").data_specs[0].name == "example-data"


def test_catalog_rejects_backup_export_targeting_unknown_compose_service(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"][0] = "${EXAMPLE_DATA_SOURCE}:/var/lib/example"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["stateful"] = True
    data["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
            "snapshot": False,
        }
    ]
    data["backup_exports"] = [
        {
            "artifact": "example.sql.gz",
            "strategy": "container",
            "container": "missing-db",
            "command": ["dump-all"],
        }
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="backup export targets unknown Compose service 'missing-db'"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_undeclared_writable_mount(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"][0] = "${EXAMPLE_DATA_SOURCE:-./data/example}:/var/lib/example"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="undeclared writable Compose mount"):
        load_service_catalog(tmp_path)


def test_catalog_matches_environment_subpath_and_long_syntax_mounts(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["example"]["volumes"] = [
        {
            "type": "bind",
            "source": "${INSTALL_ROOT:-/opt/homelab}/media",
            "target": "/data",
        },
        {
            "type": "bind",
            "source": "${INSTALL_ROOT:-/opt/homelab}/config",
            "target": "/config",
            "read_only": True,
        },
    ]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("host_sources")
    manifest["stateful"] = True
    manifest["data_specs"] = [
        {
            "name": "media-library",
            "source_env": "INSTALL_ROOT",
            "source_subpath": "media",
            "target": "/data",
            "size_estimate_gb": 0,
            "snapshot": False,
            "manage_permissions": False,
            "shared": True,
        }
    ]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    assert load_service_catalog(tmp_path).require("example").data_specs[0].source_subpath == "media"


def test_catalog_runtime_storage_declaration_does_not_cover_another_compose_service(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    compose_path = tmp_path / "example" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    writable = "${EXAMPLE_DATA_SOURCE:-./data/example}:/var/lib/example"
    compose["services"]["example"]["volumes"] = [writable]
    compose["services"]["example-agent"] = {
        "image": "example:1.0@sha256:" + ("b" * 64),
        "volumes": [writable],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    manifest_path = tmp_path / "example" / "service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtimes"] = {"example-agent": {"placements": ["apps"]}}
    manifest["stateful"] = True
    manifest["data_specs"] = [
        {
            "name": "agent-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "runtime_service": "example-agent",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
        }
    ]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="undeclared writable Compose mount"):
        load_service_catalog(tmp_path)


def test_catalog_requires_oidc_route_and_declared_client_secret(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["routes"][0]["auth"] = {"mode": "oidc"}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="OIDC manifest"):
        load_service_catalog(tmp_path)

    data["oidc"] = {
        "client_id": "example",
        "secret_env_var": "EXAMPLE_OIDC_SECRET",
        "redirect_uris": ["https://example.{domain}/auth/callback"],
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ManifestCatalogError, match="required secret"):
        load_service_catalog(tmp_path)

    data["required_secrets"] = [
        {
            "name": "EXAMPLE_OIDC_SECRET",
            "tier": "generated",
            "description": "OIDC client secret",
            "rotation": "restart",
        }
    ]
    data["routes"][0]["auth"] = {"mode": "forward_auth"}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ManifestCatalogError, match="no OIDC route"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_multiple_default_routes_for_one_host(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha", subdomain="shared")
    _write_manifest(tmp_path, "beta", subdomain="shared")

    with pytest.raises(ManifestCatalogError, match="default route"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_duplicate_path_match_for_one_host(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example", subdomain="shared")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    secondary = {
        **data["routes"][0],
        "auth": {"mode": "native"},
        "match": {"kind": "exact", "paths": ["/capability"]},
    }
    data["routes"] = [secondary, secondary, data["routes"][0]]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="duplicate exact route"):
        load_service_catalog(tmp_path)


def test_catalog_rejects_split_and_secondary_exact_path_collision(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example", subdomain="shared")
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["routes"][0]["auth"] = {"mode": "split", "passthrough_paths": ["/api/v1/version"]}
    secondary = {
        **data["routes"][0],
        "auth": {"mode": "native"},
        "match": {"kind": "exact", "paths": ["/api/v1/version"]},
    }
    secondary.pop("variants", None)
    data["routes"] = [secondary, data["routes"][0]]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="duplicate exact route"):
        load_service_catalog(tmp_path)


def test_catalog_cache_can_be_cleared_explicitly(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "example")
    assert load_service_catalog(tmp_path).require("example").label == "Example"
    path = tmp_path / "example" / "service.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["label"] = "Changed"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    assert load_service_catalog(tmp_path).require("example").label == "Example"
    clear_catalog_cache()
    assert load_service_catalog(tmp_path).require("example").label == "Changed"
