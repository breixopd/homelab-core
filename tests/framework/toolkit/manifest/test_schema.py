from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError
from toolkit.core.manifest.schema import ConfigPredicate, ServiceManifest


def _manifest() -> dict:
    return {
        "name": "example",
        "label": "Example",
        "description": "Example service",
        "icon": "box",
        "category": "cloud",
        "placement": "apps",
        "priority": 30,
        "restart_policy": "careful",
        "depends_on": [],
        "memory_tier": "light",
        "routes": [
            {
                "subdomain": "example",
                "upstream": "example:8080",
                "exposure": "public",
                "auth": {"mode": "forward_auth"},
            }
        ],
    }


def test_service_manifest_is_strict_and_immutable() -> None:
    raw = _manifest()
    manifest = ServiceManifest.model_validate(raw)

    assert manifest.routes[0].auth.mode == "forward_auth"
    assert manifest.routes[0].exposure == "public"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate({**raw, "unexpected": True})
    with pytest.raises(ValidationError):
        manifest.priority = 10  # type: ignore[misc]


def test_service_manifest_accepts_plugin_defined_category_ids() -> None:
    raw = _manifest()
    raw["category"] = "photos"

    assert ServiceManifest.model_validate(raw).category == "photos"

    raw["category"] = "Bad Category"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_host_integration_uses_native_ansible_role_identifiers() -> None:
    raw = _manifest()
    raw["host_integrations"] = [
        {
            "id": "example-agent",
            "label": "Example agent",
            "kinds": ["plain"],
            "ansible_role": "example_agent",
        }
    ]

    assert ServiceManifest.model_validate(raw).host_integrations[0].ansible_role == "example_agent"

    raw["host_integrations"][0]["ansible_role"] = "example-agent"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_service_runtime_is_explicit_for_embedded_services() -> None:
    raw = _manifest()
    assert ServiceManifest.model_validate(raw).runtime == "container"

    raw["runtime"] = "embedded"
    assert ServiceManifest.model_validate(raw).runtime == "embedded"


def test_service_health_declares_starting_policy() -> None:
    raw = _manifest()
    raw["health"] = {"starting_policy": "pending"}

    assert ServiceManifest.model_validate(raw).health.starting_policy == "pending"

    raw["health"] = {"starting_policy": "healthy"}
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_image_build_contract_is_strict_and_service_relative() -> None:
    raw = _manifest()
    raw["image_build"] = {
        "context": "image",
        "env_var": "HOMELAB_EXAMPLE_IMAGE",
        "smoke_tests": [
            {"command": ["example", "--version"]},
            {"entrypoint": "python", "command": ["-c", "import app"], "contains": "ready"},
        ],
        "requirements": "requirements.txt",
    }

    image = ServiceManifest.model_validate(raw).image_build

    assert image is not None
    assert image.context == "image"
    assert image.platforms == ("linux/amd64", "linux/arm64")
    assert image.smoke_tests[1].entrypoint == "python"


@pytest.mark.parametrize("platforms", [[], ["linux/amd64", "linux/amd64"], ["linux/riscv64"]])
def test_image_build_rejects_invalid_platform_contract(platforms: list[str]) -> None:
    raw = _manifest()
    raw["image_build"] = {
        "context": "image",
        "env_var": "HOMELAB_EXAMPLE_IMAGE",
        "platforms": platforms,
    }

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "path",
    ["/image", "../image", "image/../other", "image/./nested", "image//nested", "image\\nested"],
)
def test_image_build_rejects_unsafe_paths(path: str) -> None:
    raw = _manifest()
    raw["image_build"] = {"context": path, "env_var": "HOMELAB_EXAMPLE_IMAGE"}

    with pytest.raises(ValidationError, match="normalized relative path"):
        ServiceManifest.model_validate(raw)


def test_image_build_rejects_unknown_dependency_audit_policy() -> None:
    raw = _manifest()
    raw["image_build"] = {
        "context": "image",
        "env_var": "HOMELAB_EXAMPLE_IMAGE",
        "requirements": "requirements.txt",
        "ignored_vulnerabilities": ["CVE-2026-1234"],
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ServiceManifest.model_validate(raw)


def test_image_release_contract_is_immutable_and_service_owned() -> None:
    raw = _manifest()
    raw["image_release"] = {
        "compose_service": "example",
        "repository": "ghcr.io/example/example",
        "version": "v1.2.3",
        "digest": "sha256:" + ("a" * 64),
    }

    release = ServiceManifest.model_validate(raw).image_release

    assert release is not None
    assert release.version_ref == "ghcr.io/example/example:v1.2.3"
    assert release.immutable_ref.endswith("@sha256:" + ("a" * 64))


def test_external_module_integration_contract_is_typed_and_versioned() -> None:
    raw = _manifest()
    raw["image_release"] = {
        "compose_service": "example",
        "repository": "ghcr.io/example/example",
        "version": "v1.2.3",
        "digest": "sha256:" + ("a" * 64),
    }
    raw["integration_contract"] = {
        "version": 1,
        "compatibility": "1.x",
        "capabilities": ["status", "manual-sync"],
    }

    contract = ServiceManifest.model_validate(raw).integration_contract

    assert contract is not None
    assert contract.endpoint == "/api/contract"
    assert contract.capabilities == ("status", "manual-sync")


@pytest.mark.parametrize(
    "contract",
    [
        {"version": 1, "compatibility": "2.x", "capabilities": ["status"]},
        {"version": 1, "compatibility": "1.x", "capabilities": ["status", "status"]},
        {"version": 1, "compatibility": "1.x", "capabilities": ["Bad Capability"]},
        {"version": 1, "compatibility": "1.x", "endpoint": "api/contract", "capabilities": ["status"]},
    ],
)
def test_external_module_integration_contract_rejects_invalid_values(contract: dict[str, object]) -> None:
    raw = _manifest()
    raw["image_release"] = {
        "compose_service": "example",
        "repository": "ghcr.io/example/example",
        "version": "v1.2.3",
        "digest": "sha256:" + ("a" * 64),
    }
    raw["integration_contract"] = contract

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_integration_contract_requires_external_image_ownership() -> None:
    raw = _manifest()
    raw["integration_contract"] = {"version": 1, "compatibility": "1.x", "capabilities": ["status"]}

    with pytest.raises(ValidationError, match="independently released image"):
        ServiceManifest.model_validate(raw)


def test_service_rejects_build_and_release_image_ownership_together() -> None:
    raw = _manifest()
    raw["image_build"] = {"context": "image", "env_var": "HOMELAB_EXAMPLE_IMAGE"}
    raw["image_release"] = {
        "compose_service": "example",
        "repository": "ghcr.io/example/example",
        "version": "v1.2.3",
        "digest": "sha256:" + ("a" * 64),
    }

    with pytest.raises(ValidationError, match="exactly one image ownership contract"):
        ServiceManifest.model_validate(raw)


def test_host_sources_are_strict_and_support_typed_path_variants() -> None:
    raw = _manifest()
    raw["host_sources"] = {
        "EXAMPLE_DATA_SOURCE": {
            "path": "data/example",
            "variants": [
                {
                    "when": {"setting": "example.cache", "equals": True},
                    "path": "data/example-cache",
                }
            ],
        }
    }
    raw["management"] = {"settings": [{"key": "cache", "label": "Cache", "type": "boolean", "default": False}]}

    source = ServiceManifest.model_validate(raw).host_sources["EXAMPLE_DATA_SOURCE"]

    assert source.path == "data/example"
    assert source.variants[0].path == "data/example-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/data/example",
        "../data",
        "data/./example",
        "data//example",
        "data\\example",
        "config",
        "config/x;touch-pwned",
        "config/x $(id)",
    ],
)
def test_host_sources_reject_unsafe_paths(path: str) -> None:
    raw = _manifest()
    raw["host_sources"] = {"EXAMPLE_DATA_SOURCE": {"path": path}}

    with pytest.raises(ValidationError, match="normalized relative path"):
        ServiceManifest.model_validate(raw)


def test_host_sources_reject_duplicate_matching_variants() -> None:
    raw = _manifest()
    predicate = {"path": "network.expose_via_internet", "equals": True}
    raw["host_sources"] = {
        "EXAMPLE_DATA_SOURCE": {
            "path": "data/example",
            "variants": [
                {"when": predicate, "path": "data/one"},
                {"when": predicate, "path": "data/two"},
            ],
        }
    }

    with pytest.raises(ValidationError, match="unique predicates"):
        ServiceManifest.model_validate(raw)


def test_generated_artifacts_are_strict_and_typed() -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [
        {"path": "generated/example.conf", "sensitive": True},
        {"path": "generated/example-health.sh", "executable": True},
        {"path": "generated/example-current", "kind": "symlink"},
    ]

    artifacts = ServiceManifest.model_validate(raw).generated_artifacts

    assert artifacts[0].sensitive is True
    assert artifacts[1].executable is True
    assert artifacts[2].kind == "symlink"

    raw["generated_artifacts"] = [
        {"path": "generated/rootless-secret.json", "sensitive": True, "host_uid": 1000, "host_gid": 1000}
    ]
    artifact = ServiceManifest.model_validate(raw).generated_artifacts[0]
    assert (artifact.host_uid, artifact.host_gid) == (1000, 1000)


@pytest.mark.parametrize(
    "artifact",
    [
        {"path": "generated/example", "host_uid": 1000},
        {"path": "generated/example", "host_gid": 1000},
    ],
)
def test_generated_artifact_owner_requires_uid_and_gid(artifact: dict[str, object]) -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [artifact]

    with pytest.raises(ValidationError, match="requires both host_uid and host_gid"):
        ServiceManifest.model_validate(raw)


def test_generated_symlink_rejects_file_metadata() -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [{"path": "generated/example-current", "kind": "symlink", "mode": "0600"}]

    with pytest.raises(ValidationError, match="cannot declare file metadata"):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "path",
    ["/generated/example", "../generated/example", "generated/../example", "generated//example", "generated\\example"],
)
def test_generated_artifacts_reject_unsafe_paths(path: str) -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [{"path": path}]

    with pytest.raises(ValidationError, match="normalized relative path"):
        ServiceManifest.model_validate(raw)


def test_generated_artifacts_reject_duplicate_paths_and_invalid_modes() -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [
        {"path": "generated/example", "sensitive": True},
        {"path": "generated/example", "executable": True},
    ]

    with pytest.raises(ValidationError, match="unique"):
        ServiceManifest.model_validate(raw)

    raw["generated_artifacts"] = [
        {"path": "generated/example", "kind": "symlink", "sensitive": True},
    ]
    with pytest.raises(ValidationError, match="symlink"):
        ServiceManifest.model_validate(raw)


def test_database_provider_and_consumer_bindings_are_typed() -> None:
    raw = _manifest()
    raw["database_provider"] = {
        "engine": "postgresql",
        "admin_username_env": "POSTGRES_USER",
        "admin_password_env": "POSTGRES_PASSWORD",
        "admin_database_env": "POSTGRES_DB",
    }
    raw["service_endpoint"] = {"container_port": 5432, "published_port": 5433}
    raw["variables"] = {"POSTGRES_USER": "admin", "POSTGRES_DB": "postgres"}
    raw["required_secrets"] = [
        {
            "name": "POSTGRES_PASSWORD",
            "tier": "generated",
            "description": "administrator password",
            "rotation": "reconcile",
        },
        {
            "name": "APP_DB_PASSWORD",
            "tier": "generated",
            "description": "application password",
            "rotation": "reconcile",
        },
    ]
    raw["databases"] = [
        {
            "provider": "postgres",
            "database": "example",
            "username": "example",
            "host_env": "APP_DB_HOST",
            "port_env": "APP_DB_PORT",
            "database_env": "APP_DB_NAME",
            "username_env": "APP_DB_USER",
            "password_env": "APP_DB_PASSWORD",
        }
    ]

    manifest = ServiceManifest.model_validate(raw)
    provider = manifest.database_provider

    assert provider is not None
    assert provider.engine == "postgresql"
    assert manifest.service_endpoint is not None
    assert manifest.service_endpoint.container_port == 5432
    assert manifest.databases[0].provider == "postgres"
    assert manifest.databases[0].host_env == "APP_DB_HOST"
    assert manifest.databases[0].port_env == "APP_DB_PORT"
    assert manifest.databases[0].database_env == "APP_DB_NAME"
    assert manifest.databases[0].username_env == "APP_DB_USER"
    assert manifest.databases[0].password_env == "APP_DB_PASSWORD"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate({**raw, "database_provider": {"engine": "mysql", "container_port": 3306}})


def test_database_contract_rejects_duplicate_bindings_and_undeclared_local_environment() -> None:
    raw = _manifest()
    binding = {
        "provider": "postgres",
        "database": "example",
        "username": "example",
        "host_env": "APP_DB_HOST",
        "port_env": "APP_DB_PORT",
        "database_env": "APP_DB_NAME",
        "username_env": "APP_DB_USER",
        "password_env": "APP_DB_PASSWORD",
    }
    raw["databases"] = [binding, binding]

    with pytest.raises(ValidationError, match="database bindings must be unique"):
        ServiceManifest.model_validate(raw)

    raw["databases"] = [binding]
    with pytest.raises(ValidationError, match="undeclared password environment"):
        ServiceManifest.model_validate(raw)


def test_service_integrations_are_typed_and_namespaced() -> None:
    raw = _manifest()
    raw["depends_on"] = ["cache"]
    raw["integrations"] = [
        {
            "service": "cache",
            "host_env": "EXAMPLE_CACHE_HOST",
            "port_env": "EXAMPLE_CACHE_PORT",
        }
    ]

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.integrations[0].service == "cache"
    assert manifest.integrations[0].host_env == "EXAMPLE_CACHE_HOST"
    assert manifest.integrations[0].port_env == "EXAMPLE_CACHE_PORT"


def test_optional_service_integration_supports_compound_outputs() -> None:
    raw = _manifest()
    raw["integrations"] = [
        {
            "service": "notifications",
            "required": False,
            "enabled_env": "EXAMPLE_NOTIFICATIONS_ENABLED",
            "url_env": "EXAMPLE_NOTIFICATIONS_URL",
            "scheme": "http",
        }
    ]

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.integrations[0].required is False
    assert manifest.integrations[0].url_env == "EXAMPLE_NOTIFICATIONS_URL"


def test_required_service_integration_requires_readiness_outputs() -> None:
    raw = _manifest()
    raw["integrations"] = [{"service": "cache", "address_env": "EXAMPLE_CACHE_ADDRESS"}]

    with pytest.raises(ValidationError, match="required service integration requires host and port"):
        ServiceManifest.model_validate(raw)


def test_service_integration_environment_outputs_are_unique_across_manifest() -> None:
    raw = _manifest()
    raw["integrations"] = [
        {"service": "cache", "host_env": "EXAMPLE_HOST", "port_env": "EXAMPLE_CACHE_PORT"},
        {"service": "search", "host_env": "EXAMPLE_HOST", "port_env": "EXAMPLE_SEARCH_PORT"},
    ]

    with pytest.raises(ValidationError, match="service integration environment outputs must be unique"):
        ServiceManifest.model_validate(raw)


def test_service_secret_projection_is_typed_and_renames_runtime_environment() -> None:
    raw = _manifest()
    raw["secret_projections"] = [{"source_env": "EXTERNAL_API_TOKEN", "target_env": "EXAMPLE_API_TOKEN"}]

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.secret_projections[0].source_env == "EXTERNAL_API_TOKEN"
    assert manifest.secret_projections[0].target_env == "EXAMPLE_API_TOKEN"


def test_service_credential_can_declare_a_username_fallback() -> None:
    raw = _manifest()
    raw["credentials"] = [
        {
            "name": "Example",
            "url": "https://example.{domain}",
            "username_env": "EXAMPLE_ADMIN_USER",
            "username": "admin",
            "password_env": "EXAMPLE_ADMIN_PASSWORD",
        }
    ]

    assert ServiceManifest.model_validate(raw).credentials[0].username == "admin"


def test_required_secret_can_declare_a_runtime_fallback() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_ADMIN_PASSWORD",
            "tier": "bootstrapped",
            "description": "Initial administrator password",
            "fallback_env": "OWNER_PASSWORD",
        }
    ]

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.required_secrets[0].fallback_env == "OWNER_PASSWORD"


def test_required_secret_runtime_fallback_must_be_distinct() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_ADMIN_PASSWORD",
            "tier": "bootstrapped",
            "description": "Initial administrator password",
            "fallback_env": "EXAMPLE_ADMIN_PASSWORD",
        }
    ]

    with pytest.raises(ValidationError, match="fallback source must differ"):
        ServiceManifest.model_validate(raw)


def test_generated_secret_can_declare_a_fixed_default() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_ADMIN_USER",
            "tier": "generated",
            "description": "Initial administrator username",
            "default": "admin",
            "rotation": "persistent",
        }
    ]

    assert ServiceManifest.model_validate(raw).required_secrets[0].default == "admin"

    raw["required_secrets"][0]["tier"] = "user"
    with pytest.raises(ValidationError, match="fixed defaults require the generated tier"):
        ServiceManifest.model_validate(raw)


def test_generated_secret_requires_explicit_rotation_policy() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_STORAGE_KEY",
            "tier": "generated",
            "description": "Persistent encryption key",
            "rotation": "persistent",
        }
    ]

    assert ServiceManifest.model_validate(raw).required_secrets[0].rotation == "persistent"

    del raw["required_secrets"][0]["rotation"]
    with pytest.raises(ValidationError, match="explicitly declare a rotation policy"):
        ServiceManifest.model_validate(raw)


def test_generated_secret_rotation_policy_is_bounded() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_KEY",
            "tier": "generated",
            "description": "Signing key",
            "rotation": "restart",
        }
    ]
    assert ServiceManifest.model_validate(raw).required_secrets[0].rotation == "restart"
    raw["required_secrets"][0]["rotation"] = "unknown"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_generated_artifacts_must_live_under_generated_tree() -> None:
    raw = _manifest()
    raw["generated_artifacts"] = [{"path": "config/example.yml"}]

    with pytest.raises(ValidationError, match="generated/"):
        ServiceManifest.model_validate(raw)


def test_generated_secret_can_require_a_complex_password_generator() -> None:
    raw = _manifest()
    raw["required_secrets"] = [
        {
            "name": "EXAMPLE_PASSWORD",
            "tier": "generated",
            "description": "Service password",
            "generator": "password",
            "rotation": "reconcile",
        }
    ]

    assert ServiceManifest.model_validate(raw).required_secrets[0].generator == "password"

    raw["required_secrets"][0]["tier"] = "user"
    with pytest.raises(ValidationError, match="password generators require"):
        ServiceManifest.model_validate(raw)


def test_bundled_catalog_declares_rotation_for_every_generated_secret() -> None:
    from toolkit.core.manifest.catalog import load_service_catalog

    manifests = load_service_catalog().manifests
    generated = [secret for manifest in manifests for secret in manifest.required_secrets if secret.tier == "generated"]
    assert generated
    assert all("rotation" in secret.model_fields_set for secret in generated)


def test_service_runtime_variables_are_bounded_environment_names() -> None:
    raw = _manifest()
    raw["runtime_variables"] = ["EXAMPLE_WORKERS"]

    assert ServiceManifest.model_validate(raw).runtime_variables == ("EXAMPLE_WORKERS",)

    raw["runtime_variables"] = ["EXAMPLE_WORKERS", "EXAMPLE_WORKERS"]
    with pytest.raises(ValidationError, match="runtime variables must be unique"):
        ServiceManifest.model_validate(raw)


def test_service_manifest_rejects_category_lifecycle_phases() -> None:
    raw = _manifest()
    raw["post_start_phase"] = "after_category"

    with pytest.raises(ValidationError, match="post_start_phase"):
        ServiceManifest.model_validate(raw)


def test_runtime_services_are_strict_and_declarative() -> None:
    raw = _manifest()
    raw["runtimes"] = {
        "example-agent": {
            "placements": ["@non-primary", "apps"],
            "compose_profile": "example-agents",
            "mode": "oneshot",
            "required_host_paths": ["/dev/dri"],
        }
    }

    manifest = ServiceManifest.model_validate(raw)

    runtime = manifest.runtimes["example-agent"]
    assert runtime.placements == ("@non-primary", "apps")
    assert runtime.compose_profile == "example-agents"
    assert runtime.mode == "oneshot"
    assert runtime.required_host_paths == ("/dev/dri",)
    for invalid in (
        {"Bad_Service": {}},
        {"example-agent": {"placements": ["apps", "apps"]}},
        {"example-agent": {"placements": ["Invalid_Node"]}},
        {"example-agent": {"placements": ["@unknown"]}},
        {"example-agent": {"compose_profile": "Invalid Profile"}},
        {"example-agent": {"required_host_paths": ["dev/dri"]}},
        {"example-agent": {"required_host_paths": ["/dev/../dri"]}},
        {"example-agent": {"required_host_paths": ["/dev/dri", "/dev/dri"]}},
    ):
        raw["runtimes"] = invalid
        with pytest.raises(ValidationError):
            ServiceManifest.model_validate(raw)


def test_network_listeners_are_typed_and_reference_declared_runtimes() -> None:
    raw = _manifest()
    raw["runtimes"] = {"example-agent": {"placements": ["@non-primary"]}}
    raw["network_listeners"] = [
        {
            "id": "agent-metrics",
            "port": 9100,
            "runtime_service": "example-agent",
            "sources": ["@service:prometheus", "observability"],
        }
    ]

    listener = ServiceManifest.model_validate(raw).network_listeners[0]

    assert listener.runtime_service == "example-agent"
    assert listener.sources == ("@service:prometheus", "observability")

    raw["network_listeners"][0]["runtime_service"] = "missing-agent"
    with pytest.raises(ValidationError, match="referenced runtime"):
        ServiceManifest.model_validate(raw)

    raw["network_listeners"][0]["runtime_service"] = "example-agent"
    raw["network_listeners"][0]["sources"] = ["@runtime:missing-agent"]
    with pytest.raises(ValidationError, match="source runtime"):
        ServiceManifest.model_validate(raw)


def test_network_listeners_support_public_sources_and_typed_conditions() -> None:
    raw = _manifest()
    raw["network_listeners"] = [
        {
            "id": "public-api",
            "port": 8443,
            "sources": ["@lan", "@mesh", "@internet"],
            "enabled_when": [{"path": "network.expose_via_internet", "equals": True}],
        }
    ]

    listener = ServiceManifest.model_validate(raw).network_listeners[0]

    assert listener.sources == ("@lan", "@mesh", "@internet")
    assert listener.enabled_when == (ConfigPredicate(path="network.expose_via_internet", equals=True),)


def test_host_process_listener_cannot_claim_a_compose_runtime() -> None:
    raw = _manifest()
    raw["network_listeners"] = [
        {
            "id": "host-api",
            "port": 1514,
            "runtime_service": "example",
            "host_process": True,
            "sources": ["@all"],
        }
    ]

    with pytest.raises(ValidationError, match="host-process"):
        ServiceManifest.model_validate(raw)


def test_stateful_service_requires_strict_storage_assets() -> None:
    raw = _manifest()
    raw["stateful"] = True

    with pytest.raises(ValidationError, match="storage asset"):
        ServiceManifest.model_validate(raw)

    raw["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 10,
        }
    ]
    manifest = ServiceManifest.model_validate(raw)

    assert manifest.data_specs[0].source_env == "EXAMPLE_DATA_SOURCE"
    assert manifest.data_specs[0].target == "/var/lib/example"


def test_backup_exports_are_typed_and_owned_by_storage_manifests() -> None:
    raw = _manifest()
    raw["stateful"] = True
    raw["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
            "snapshot": False,
        }
    ]
    raw["backup_exports"] = [
        {
            "artifact": "example.sqlite.gz",
            "strategy": "sqlite",
            "data_spec": "example-data",
            "database_path": "db.sqlite",
        }
    ]

    export = ServiceManifest.model_validate(raw).backup_exports[0]

    assert export.strategy == "sqlite"
    assert export.data_spec == "example-data"
    assert export.database_path == "db.sqlite"


def test_backup_exports_require_a_stateful_service() -> None:
    raw = _manifest()
    raw["backup_exports"] = [
        {
            "artifact": "example.sql.gz",
            "strategy": "container",
            "command": ["pg_dumpall"],
        }
    ]

    with pytest.raises(ValidationError, match="stateful"):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "backup_export",
    [
        {
            "artifact": "other.sqlite.gz",
            "strategy": "sqlite",
            "data_spec": "example-data",
            "database_path": "db.sqlite",
        },
        {
            "artifact": "example.sqlite.gz",
            "strategy": "sqlite",
            "data_spec": "missing-data",
            "database_path": "db.sqlite",
        },
        {
            "artifact": "example.sqlite.gz",
            "strategy": "sqlite",
            "data_spec": "example-data",
            "database_path": "../db.sqlite",
        },
        {
            "artifact": "example.sql.gz",
            "strategy": "container",
        },
        {
            "artifact": "example.sql.gz",
            "strategy": "container",
            "command": ["pg_dumpall"],
            "data_spec": "example-data",
        },
    ],
)
def test_backup_exports_reject_ambiguous_or_unsafe_contracts(backup_export: dict[str, object]) -> None:
    raw = _manifest()
    raw["stateful"] = True
    raw["data_specs"] = [
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/var/lib/example",
            "size_estimate_gb": 1,
            "snapshot": False,
        }
    ]
    raw["backup_exports"] = [backup_export]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_storage_asset_supports_declared_shared_environment_subpath() -> None:
    raw = _manifest()
    raw["stateful"] = True
    raw["data_specs"] = [
        {
            "name": "media-library",
            "source_env": "INSTALL_ROOT",
            "source_subpath": "media/library",
            "target": "/data",
            "size_estimate_gb": 0,
            "snapshot": False,
            "manage_permissions": False,
            "shared": True,
        }
    ]

    asset = ServiceManifest.model_validate(raw).data_specs[0]

    assert asset.source_subpath == "media/library"
    assert asset.shared is True


def test_host_path_supports_non_recursive_setgid_layout() -> None:
    raw = _manifest()
    raw["host_paths"] = [
        {
            "path": "media",
            "uid": 1000,
            "gid": 1001,
            "mode": "2775",
            "subdirs": ["tv", "movies"],
            "create": True,
            "recursive": False,
        }
    ]

    host_path = ServiceManifest.model_validate(raw).host_paths[0]

    assert host_path.mode == "2775"
    assert host_path.recursive is False


@pytest.mark.parametrize("mode", ["755", "0888", "27a5", "47555"])
def test_host_path_rejects_invalid_modes(mode: str) -> None:
    raw = _manifest()
    raw["host_paths"] = [{"path": "media", "mode": mode}]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_subpath": "../media"},
        {"source_subpath": "/media"},
        {"host_subdirs": ["../escape"]},
        {"host_subdirs": ["valid", "valid"]},
        {"volume": "example-data", "source_env": None, "source_subpath": "nested"},
        {"shared": True},
        {"shared": True, "snapshot": False},
        {"shared": True, "snapshot": False, "manage_permissions": False, "size_estimate_gb": 1},
    ],
)
def test_storage_asset_rejects_unsafe_subpaths_and_managed_shared_data(overrides: dict[str, object]) -> None:
    raw = _manifest()
    raw["stateful"] = True
    asset: dict[str, object] = {
        "name": "example-data",
        "source_env": "EXAMPLE_DATA_SOURCE",
        "target": "/data",
        "size_estimate_gb": 0,
    }
    asset.update(overrides)
    raw["data_specs"] = [asset]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_storage_asset_secondary_runtime_must_have_declared_placement() -> None:
    raw = _manifest()
    raw["stateful"] = True
    raw["data_specs"] = [
        {
            "name": "agent-cache",
            "source_env": "AGENT_CACHE_SOURCE",
            "target": "/cache",
            "runtime_service": "example-agent",
            "size_estimate_gb": 1,
            "snapshot": False,
        }
    ]

    with pytest.raises(ValidationError, match="runtime is not declared"):
        ServiceManifest.model_validate(raw)

    raw["runtimes"] = {"example-agent": {"placements": ["media", "apps"]}}
    assert ServiceManifest.model_validate(raw).data_specs[0].runtime_service == "example-agent"


@pytest.mark.parametrize(
    "data_spec",
    [
        {
            "name": "example-data",
            "target": "/data",
            "size_estimate_gb": 1,
        },
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "volume": "example-data",
            "target": "/data",
            "size_estimate_gb": 1,
        },
        {
            "name": "Bad Name",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "/data",
            "size_estimate_gb": 1,
        },
        {
            "name": "example-data",
            "source_env": "lowercase",
            "target": "/data",
            "size_estimate_gb": 1,
        },
        {
            "name": "example-data",
            "source_env": "EXAMPLE_DATA_SOURCE",
            "target": "relative/data",
            "size_estimate_gb": 1,
        },
    ],
)
def test_storage_assets_reject_ambiguous_or_malformed_sources(data_spec: dict[str, object]) -> None:
    raw = _manifest()
    raw["stateful"] = True
    raw["data_specs"] = [data_spec]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_manifest_variables_accept_only_environment_names_and_safe_config_paths() -> None:
    raw = _manifest()
    raw["variables"] = {
        "EXAMPLE_HOST": "example.{config.domain}",
        "PUID": "{config.runtime.puid}",
    }
    assert ServiceManifest.model_validate(raw).variables["EXAMPLE_HOST"] == "example.{config.domain}"

    for invalid in (
        {"lowercase": "value"},
        {"EXAMPLE": "{config._private}"},
        {"EXAMPLE": "{config.runtime..puid}"},
        {"EXAMPLE": "{unknown.value}"},
        {"EXAMPLE": "bad\nvalue"},
    ):
        raw["variables"] = invalid
        with pytest.raises(ValidationError):
            ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exposure", "internal"),
        ("exposure", "mesh"),
        ("auth", False),
        ("auth", True),
        ("auth", {}),
        ("upstream", "https://example:8080"),
        ("upstream", "example"),
        ("upstream", "example:70000"),
    ],
)
def test_route_rejects_noncanonical_or_malformed_values(field: str, value: object) -> None:
    raw = _manifest()
    raw["routes"][0][field] = value

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/version",
        "/api/v1/*",
        "/api/v1/{name}",
        "/api/v1/version?x=1",
        "/api//version",
        "/api/../version",
        "/api/%76ersion",
    ],
)
def test_split_auth_rejects_nonexact_paths(path: str) -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {"mode": "split", "passthrough_paths": [path]}

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_split_auth_requires_unique_paths_and_other_modes_forbid_them() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {"mode": "split", "passthrough_paths": []}
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)

    raw["routes"][0]["auth"] = {
        "mode": "split",
        "passthrough_paths": ["/api/v1/version", "/api/v1/version"],
    }
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)

    raw["routes"][0]["auth"] = {"mode": "native", "passthrough_paths": ["/api/v1/version"]}
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_split_auth_contract_is_accepted() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {
        "mode": "split",
        "passthrough_paths": ["/api/v1/version", "/api/v1/version/"],
    }

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.routes[0].auth.mode == "split"
    assert manifest.routes[0].auth.passthrough_paths == ("/api/v1/version", "/api/v1/version/")


def test_split_auth_accepts_explicit_probe_statuses() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {
        "mode": "split",
        "passthrough_paths": ["/api/oauth/openid"],
        "probe_statuses": [500],
    }

    manifest = ServiceManifest.model_validate(raw)

    assert manifest.routes[0].auth.probe_statuses == (500,)


def test_split_auth_accepts_side_effect_free_probe_method() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {
        "mode": "split",
        "passthrough_paths": ["/api/oauth/openid"],
        "probe_method": "HEAD",
        "probe_statuses": [405],
    }

    auth = ServiceManifest.model_validate(raw).routes[0].auth

    assert auth.probe_method == "HEAD"


@pytest.mark.parametrize("statuses", [[99], [600], [500, 500]])
def test_split_auth_rejects_invalid_probe_statuses(statuses: list[int]) -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {
        "mode": "split",
        "passthrough_paths": ["/api/oauth/openid"],
        "probe_statuses": statuses,
    }

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_non_split_auth_forbids_probe_statuses() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {"mode": "native", "probe_statuses": [500]}

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_config_predicate_requires_one_safe_comparison() -> None:
    assert ConfigPredicate(path="network.expose_via_internet", equals=True).equals is True
    assert ConfigPredicate(setting="media-library.server", equals="jellyfin").setting == "media-library.server"
    assert ConfigPredicate(setting="media-cache.uplink-mbps", equals=12.5).equals == 12.5
    assert ConfigPredicate(path="dns.provider", one_of=("cloudflare", "local")).one_of == ("cloudflare", "local")

    for raw in (
        {"path": "network.expose_via_internet"},
        {"setting": "media-library.server"},
        {"path": "network.expose_via_internet", "setting": "gluetun.enabled", "equals": True},
        {"path": "network.expose_via_internet", "equals": True, "one_of": [True]},
        {"path": "network._secret", "equals": True},
        {"setting": "media-library._secret", "equals": True},
        {"setting": "Media Library.server", "equals": "jellyfin"},
        {"setting": "media-cache.uplink-mbps", "equals": float("inf")},
        {"path": "model_dump", "equals": True},
        {"path": "network..expose_via_internet", "equals": True},
    ):
        with pytest.raises(ValidationError):
            ConfigPredicate.model_validate(raw)


def test_secondary_route_match_requires_safe_unique_paths() -> None:
    raw = deepcopy(_manifest())
    raw["routes"].insert(
        0,
        {
            "subdomain": "example",
            "upstream": "example:8080",
            "exposure": "public",
            "auth": {"mode": "native"},
            "match": {"kind": "prefix", "paths": ["/api/"]},
        },
    )
    assert ServiceManifest.model_validate(raw).routes[0].match is not None

    raw["routes"][0]["match"]["paths"] = ["/api/*"]
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_prefix_route_matches_require_a_path_boundary() -> None:
    raw = deepcopy(_manifest())
    raw["routes"].insert(
        0,
        {
            "subdomain": "example",
            "upstream": "example:8080",
            "exposure": "public",
            "auth": {"mode": "native"},
            "match": {"kind": "prefix", "paths": ["/api"]},
        },
    )

    with pytest.raises(ValidationError, match="trailing slash"):
        ServiceManifest.model_validate(raw)

    raw["routes"][0]["match"]["paths"] = ["/"]
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_upstream_rejects_empty_dns_labels() -> None:
    raw = _manifest()
    raw["routes"][0]["upstream"] = "bad..host:8080"

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_oidc_redirects_allow_https_and_reverse_domain_native_app_schemes() -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {"mode": "oidc"}
    raw["oidc"] = {
        "client_id": "example",
        "secret_env_var": "EXAMPLE_OIDC_SECRET",
        "redirect_uris": ["https://example.{domain}/callback", "com.example.mobile:///oauth-callback"],
    }
    raw["required_secrets"] = [
        {"name": "EXAMPLE_OIDC_SECRET", "tier": "generated", "description": "OIDC secret", "rotation": "restart"}
    ]

    assert ServiceManifest.model_validate(raw).oidc is not None

    raw["oidc"]["redirect_uris"] = ["http://example.test/callback"]
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "custom:/oauth-callback",
        "file:///oauth-callback",
        "com.example.mobile://authority/oauth-callback",
        "com.example.mobile:///oauth-callback?token=value",
        "https://operator@example.test/oauth-callback",
        "https://example.test/oauth-callback\nmalicious",
    ],
)
def test_oidc_redirects_reject_unsafe_native_and_https_uris(redirect_uri: str) -> None:
    raw = _manifest()
    raw["routes"][0]["auth"] = {"mode": "oidc"}
    raw["oidc"] = {
        "client_id": "example",
        "secret_env_var": "EXAMPLE_OIDC_SECRET",
        "redirect_uris": [redirect_uri],
    }
    raw["required_secrets"] = [
        {"name": "EXAMPLE_OIDC_SECRET", "tier": "generated", "description": "OIDC secret", "rotation": "restart"}
    ]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)


def test_route_response_headers_reject_control_characters() -> None:
    raw = _manifest()
    raw["routes"][0]["response_headers"] = [{"name": "Content-Security-Policy", "value": "default-src 'self'"}]
    assert ServiceManifest.model_validate(raw).routes[0].response_headers[0].name == "Content-Security-Policy"

    raw["routes"][0]["response_headers"] = [{"name": "X-Test", "value": "safe\nmalicious"}]

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(raw)
