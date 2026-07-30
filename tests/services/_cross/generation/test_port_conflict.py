from __future__ import annotations

from toolkit.core.compose.port_conflict import check_container_name, check_port_conflict
from toolkit.core.config.config import Config, ProjectEntry

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _project(*, target: str = "apps", port: int = 45678) -> ProjectEntry:
    return ProjectEntry(
        name="Demo",
        subdomain="demo",
        auth_mode="forward_auth",
        exposure="private",
        docker_image=PINNED_IMAGE,
        placement=target,
        container_port=port,
    )


def test_core_port_conflicts_are_discovered_per_node() -> None:
    cfg = Config()

    assert check_port_conflict("infra", 443, cfg) == ["caddy"]
    assert check_port_conflict("apps", 8083, cfg) == ["nextcloud"]
    assert check_port_conflict("apps", 8084, cfg) == ["fmd-server"]
    assert check_port_conflict("apps", 9101, cfg) == ["fmd-server"]
    assert "jellyfin-nvidia" in check_port_conflict("media", 8096, cfg)
    assert "seaweedfs" in check_port_conflict("apps", 8333, cfg)
    assert check_port_conflict("media", 4533, cfg) == ["navidrome"]
    assert check_port_conflict("apps", 443, cfg) == []


def test_existing_project_port_conflict_is_node_scoped() -> None:
    cfg = Config()
    cfg.projects.entries = [_project()]

    assert check_port_conflict("apps", 45678, cfg) == ["Demo"]
    assert check_port_conflict("media", 45678, cfg) == []


def test_missing_config_is_supported() -> None:
    assert check_port_conflict("apps", 45678, None) == []
    assert check_container_name("demo", None) is None


def test_service_names_are_reserved_from_manifest_catalog() -> None:
    assert check_container_name("caddy", Config()) == "caddy"
    assert check_container_name("authelia", Config()) == "authelia"
    assert check_container_name("homelab-controller", Config()) == "homelab-controller"
    assert check_container_name("jellyfin-nvidia", Config()) == "jellyfin-nvidia"


def test_existing_project_identity_is_reserved_case_insensitively() -> None:
    cfg = Config()
    cfg.projects.entries = [_project()]

    assert check_container_name("DEMO", cfg) == "DEMO"
