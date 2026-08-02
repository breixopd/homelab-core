from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml
from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig, ServicesConfig
from toolkit.core.generate.compose_assemble import (
    _inject_oidc_split_dns,
    assemble_compose_text,
    assemble_role_compose_text,
    write_assembled_compose,
    write_role_compose_models,
)
from toolkit.core.manifest.catalog import ManifestCatalogError

PINNED_IMAGE = "ghcr.io/example/status:1@sha256:" + "a" * 64


def test_oidc_split_dns_preserves_plugin_extra_hosts() -> None:
    services = {"client": {"extra_hosts": ["database=192.0.2.20"]}}

    _inject_oidc_split_dns(Path.cwd(), Config(domain="example.com"), "client", services)

    assert services["client"]["extra_hosts"] == [
        "database=192.0.2.20",
        "auth.example.com=10.10.10.10",
    ]


def test_oidc_split_dns_rejects_a_conflicting_plugin_mapping() -> None:
    services = {"client": {"extra_hosts": {"auth.example.com": "192.0.2.30"}}}

    with pytest.raises(ValueError, match="expected private ingress"):
        _inject_oidc_split_dns(Path.cwd(), Config(domain="example.com"), "client", services)


def _write_service(root: Path, owner: str, services: dict) -> None:
    directory = root / "toolkit" / "services" / owner
    directory.mkdir(parents=True)
    (directory / "service.yaml").write_text(
        yaml.safe_dump(
            {
                "name": owner,
                "label": owner.title(),
                "description": f"{owner} service",
                "icon": "box",
                "category": "management",
                "placement": "control",
                "priority": 10,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for service in services.values():
        if isinstance(service, dict):
            image = service.get("image")
            if isinstance(image, str) and "@sha256:" not in image:
                service["image"] = f"{image}@sha256:{'a' * 64}"
            service.setdefault(
                "logging",
                {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            )
    (directory / "compose.yaml").write_text(
        yaml.safe_dump({"services": services}, sort_keys=False),
        encoding="utf-8",
    )


def _write_platform(root: Path) -> None:
    stacks = root / "stacks"
    stacks.mkdir(parents=True)
    (stacks / "platform.yaml").write_text(
        "networks:\n  platform-backplane:\n    name: homelab_platform_backplane\nvolumes:\n  controller-data:\n",
        encoding="utf-8",
    )


def _make_repository(root: Path) -> None:
    _write_platform(root)
    _write_service(
        root,
        "grafana",
        {"grafana": {"image": "grafana/grafana:13", "profiles": ["management"]}},
    )
    _write_service(
        root,
        "homelab-ui",
        {
            "homelab-controller": {"image": "homelab:1", "profiles": ["management"]},
            "homelab-ui": {"image": "homelab:1", "profiles": ["management"]},
        },
    )


def test_assemble_loads_service_owned_compose_applications() -> None:
    root = Path.cwd()
    data = yaml.safe_load(assemble_compose_text(root))
    expected_services: set[str] = set()
    for compose_path in (root / "toolkit/services").glob("*/compose.yaml"):
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        expected_services.update(document.get("services", {}))

    assert set(data["services"]) == expected_services
    assert "fmd-server" in data["services"]
    assert "jellyfin-nvidia" in data["services"]
    assert "immich-redis" in data["services"]
    assert "homelab-controller" in data["services"]


def test_single_node_model_omits_disabled_service_applications() -> None:
    defaults = Config()
    services = ServicesConfig.model_validate({**defaults.services.model_dump(), "media": False})
    cfg = Config.model_validate({**defaults.model_dump(mode="python"), "services": services})

    data = yaml.safe_load(assemble_compose_text(Path.cwd(), cfg))

    assert "homelab-controller" in data["services"]
    assert "jellyfin" not in data["services"]
    assert "music-sync" not in data["services"]
    assert "qbittorrent" not in data["services"]


def test_assemble_supports_multiple_runtime_services_owned_by_one_folder(tmp_path: Path) -> None:
    _make_repository(tmp_path)

    data = yaml.safe_load(assemble_compose_text(tmp_path))

    assert set(data["services"]) == {"grafana", "homelab-controller", "homelab-ui"}


def test_assemble_merges_fixed_platform_resources(tmp_path: Path) -> None:
    _make_repository(tmp_path)

    data = yaml.safe_load(assemble_compose_text(tmp_path))

    assert data["networks"]["platform-backplane"]["name"] == "homelab_platform_backplane"
    assert "controller-data" in data["volumes"]


def test_assemble_rejects_duplicate_service_ownership(tmp_path: Path) -> None:
    _write_platform(tmp_path)
    _write_service(tmp_path, "alpha", {"shared": {"image": "example/alpha:1"}})
    _write_service(tmp_path, "beta", {"shared": {"image": "example/beta:1"}})

    with pytest.raises(ValueError, match="shared"):
        assemble_compose_text(tmp_path)


def test_assemble_rejects_non_application_service_fragment(tmp_path: Path) -> None:
    _write_platform(tmp_path)
    _write_service(tmp_path, "grafana", {"grafana": {"image": "grafana/grafana:13"}})
    compose = tmp_path / "toolkit" / "services" / "grafana" / "compose.yaml"
    compose.write_text("image: grafana/grafana:13\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level services"):
        assemble_compose_text(tmp_path)


def test_assemble_rejects_empty_service_definition(tmp_path: Path) -> None:
    _write_platform(tmp_path)
    _write_service(tmp_path, "grafana", {"grafana": None})

    with pytest.raises(ValueError, match=r"services\.grafana must be a mapping"):
        assemble_compose_text(tmp_path)


def test_write_assembled_compose_creates_deterministic_file(tmp_path: Path) -> None:
    _make_repository(tmp_path)

    output = write_assembled_compose(tmp_path)

    assert output == tmp_path / "docker-compose.yml"
    assert output.is_file()
    first = output.read_text(encoding="utf-8")
    assert write_assembled_compose(tmp_path).read_text(encoding="utf-8") == first


def test_assemble_includes_declarative_projects(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    cfg = Config(
        projects=ProjectsConfig(
            entries=[
                ProjectEntry(
                    subdomain="status",
                    auth_mode="forward_auth",
                    exposure="private",
                    docker_image=PINNED_IMAGE,
                    container_port=45678,
                    placement="apps",
                )
            ]
        )
    )

    data = yaml.safe_load(assemble_compose_text(tmp_path, cfg))

    assert data["services"]["project-status"]["image"] == PINNED_IMAGE


def test_role_models_contain_only_services_assigned_to_that_node() -> None:
    cfg = Config(domain="example.com")

    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))
    media = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "media"))
    apps = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "apps"))

    assert "postgres" in infra["services"]
    assert "node-exporter" in infra["services"]
    assert "alloy-docker-proxy" in infra["services"]
    assert "alloy-agent-docker-proxy" not in infra["services"]
    assert "node-exporter-agent" not in infra["services"]
    assert "jellyfin" not in infra["services"]
    assert "fmd-server" not in infra["services"]

    assert "jellyfin" in media["services"]
    assert "node-exporter-agent" in media["services"]
    assert "cadvisor-agent" in media["services"]
    assert "alloy-agent" in media["services"]
    assert "alloy-agent-docker-proxy" in media["services"]
    assert "alloy-docker-proxy" not in media["services"]
    assert "node-exporter" not in media["services"]
    assert "postgres" not in media["services"]

    assert "fmd-server" in apps["services"]
    assert "romm" in apps["services"]
    assert apps["services"]["romm"]["image"].startswith("docker.io/rommapp/romm:5.0.0@sha256:")
    assert apps["services"]["romm"]["ports"] == ["${PRIVATE_IP:-127.0.0.1}:8090:8080"]
    assert apps["services"]["romm"]["extra_hosts"] == {"auth.example.com": "10.10.10.10"}
    assert apps["services"]["vaultwarden"]["extra_hosts"] == {"auth.example.com": "10.10.10.10"}
    assert "extra_hosts" not in apps["services"]["fmd-server"]
    assert "nextcloud" in apps["services"]
    assert "node-exporter-agent" in apps["services"]
    assert "jellyfin" not in apps["services"]
    assert "postgres" not in apps["services"]


def test_backup_agents_are_placed_on_data_nodes_only() -> None:
    cfg = Config(backups={"enabled": True})

    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))
    media = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "media"))
    apps = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "apps"))

    assert "kopia" in infra["services"]
    assert "kopia-agent" not in infra["services"]
    assert "kopia-agent" in media["services"]
    assert "kopia-agent" in apps["services"]


def test_new_workload_machine_automatically_receives_cross_node_agents() -> None:
    machines = Config().machines
    machines["worker"] = machines["apps"].model_copy(
        update={
            "hostname": "worker-01",
            "vmid": 899,
            "address": "10.10.10.99",
            "labels": ("worker",),
        }
    )
    cfg = Config(backups={"enabled": True}, machines=machines)

    worker = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "worker"))

    assert {
        "node-exporter-agent",
        "cadvisor-agent",
        "alloy-agent",
        "alloy-agent-docker-proxy",
        "kopia-agent",
    }.issubset(worker["services"])
    assert "node-exporter" not in worker["services"]
    assert "kopia" not in worker["services"]


def test_backup_mounts_include_only_declared_snapshot_assets() -> None:
    cfg = Config(backups={"enabled": True})

    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))
    media = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "media"))
    apps = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "apps"))

    infra_mounts = infra["services"]["kopia"]["volumes"]
    media_mounts = media["services"]["kopia-agent"]["volumes"]
    apps_mounts = apps["services"]["kopia-agent"]["volumes"]

    def by_target(mounts: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        return {str(mount["target"]): mount for mount in mounts if isinstance(mount, dict)}

    infra_targets = by_target(infra_mounts)
    media_targets = by_target(media_mounts)
    apps_targets = by_target(apps_mounts)

    assert infra_targets["/source/config.yaml"]["read_only"] is True
    assert infra_targets["/source/backup-dumps"]["source"] == "${KOPIA_DUMPS_SOURCE}"
    assert infra_targets["/source/backup-dumps"]["read_only"] is True
    assert infra_targets["/source/secrets.enc.yaml"]["read_only"] is True
    assert "/source/secrets.enc.yaml" not in media_targets
    assert "/source/secrets.enc.yaml" not in apps_targets
    assert infra_targets["/source/services/homelab-ui/controller-data"]["source"] == "controller-data"
    assert "/source/services/kopia/backup-repository" not in infra_targets
    assert "/source/services/kopia/kopia-cache" not in infra_targets
    assert "/source/services/postgres/postgres-data" not in infra_targets
    assert "/source/services/komodo-mongo/komodo-database" not in infra_targets
    assert "/source/services/roundcube/roundcube-data" not in infra_targets
    assert "/source/services/headscale/headscale-data" not in infra_targets
    assert media_targets["/source/services/jellyfin/jellyfin-config"]["source"] == "${JELLYFIN_CONFIG_SOURCE}"
    assert "/source/services/jellyfin/media-library" not in media_targets
    assert "/source/services/fmd-server/fmd-database" not in apps_targets
    assert "/source/services/dev-postgres/dev-postgres-data" not in apps_targets
    assert "/source/services/immich-postgres/immich-database" not in apps_targets
    assert all(target != "/source" for target in (*infra_targets, *media_targets, *apps_targets))


def test_role_models_prune_cross_node_dependencies_and_unused_resources() -> None:
    apps = yaml.safe_load(assemble_role_compose_text(Path.cwd(), Config(), "apps"))

    assert "postgres" not in apps["services"]["nextcloud"].get("depends_on", {})
    assert "redis" not in apps["services"]["nextcloud"].get("depends_on", {})
    assert "edge" not in apps["networks"]
    assert "plugin-nextcloud" in apps["networks"]
    assert "controller-data" not in apps.get("volumes", {})


def test_authelia_mailserver_integration_has_a_shared_network() -> None:
    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), Config(), "infra"))

    network = "link-authelia-mailserver"
    assert network in infra["networks"]
    assert network in infra["services"]["authelia"]["networks"]
    assert network in infra["services"]["mailserver"]["networks"]


def test_role_models_assign_unique_small_subnets_to_managed_bridges() -> None:
    cfg = Config()
    pool = ipaddress.ip_network(cfg.network.container_ipv4_cidr)

    for node in ("infra", "media", "apps"):
        model = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, node))
        allocated = []
        for name, definition in model["networks"].items():
            if name in {"caddy-egress", "prometheus-egress"}:
                continue
            subnet = ipaddress.ip_network(definition["ipam"]["config"][0]["subnet"])
            assert subnet.prefixlen == cfg.network.container_network_prefix
            assert subnet.subnet_of(pool)
            allocated.append(subnet)

        assert len(allocated) == len(set(allocated))


def test_role_model_prunes_caddy_dependency_when_security_is_disabled() -> None:
    defaults = Config()
    services = ServicesConfig.model_validate({**defaults.services.model_dump(), "security": False})
    cfg = Config.model_validate({**defaults.model_dump(mode="python"), "services": services})

    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))

    assert "caddy" in infra["services"]
    assert "crowdsec" not in infra["services"]
    assert "crowdsec" not in infra["services"]["caddy"].get("depends_on", {})


def test_role_models_root_local_build_contexts_at_install_directory() -> None:
    cfg = Config()

    infra = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "infra"))
    media = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "media"))

    assert infra["services"]["caddy"]["build"]["context"] == ("${INSTALL_ROOT:-.}/toolkit/services/caddy/image")
    assert infra["services"]["homelab-ui"]["build"]["context"] == "${INSTALL_ROOT:-.}"
    assert "build" not in media["services"]["music-sync"]
    assert media["services"]["music-sync"]["image"] == (
        "ghcr.io/breixopd/music-sync:v1.2.0@sha256:b2868c5c3561821554faabeda0ac4de7d279365152fd3297138566cfcaf0aa83"
    )


def test_role_model_places_declarative_projects_on_their_target_node() -> None:
    cfg = Config(
        projects=ProjectsConfig(
            entries=[
                ProjectEntry(
                    subdomain="status",
                    auth_mode="forward_auth",
                    exposure="private",
                    docker_image=PINNED_IMAGE,
                    container_port=45678,
                    placement="media",
                )
            ]
        )
    )

    media = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "media"))
    apps = yaml.safe_load(assemble_role_compose_text(Path.cwd(), cfg, "apps"))

    assert "project-status" in media["services"]
    assert "project-status" not in apps["services"]


def test_role_assembly_rejects_unknown_runtime_service_placement(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    manifest = tmp_path / "toolkit" / "services" / "grafana" / "service.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["runtimes"] = {"missing-agent": {"placements": ["media"]}}
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ManifestCatalogError, match="missing-agent"):
        assemble_role_compose_text(tmp_path, Config(), "media")


def test_write_role_compose_models_writes_each_enabled_multi_node_role(tmp_path: Path) -> None:
    _make_repository(tmp_path)

    outputs = write_role_compose_models(tmp_path, Config())

    assert set(outputs) == {"infra", "media", "apps"}
    assert outputs["media"] == tmp_path / "generated" / "media" / "compose.yaml"
    assert all(path.is_file() for path in outputs.values())
