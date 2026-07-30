from __future__ import annotations

from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig
from toolkit.core.deploy.compose_limits import _vm_service_names
from toolkit.core.projects.compose import project_compose_document, project_profiles_for_vm

PINNED_IMAGE = "ghcr.io/example/status:1@sha256:" + "a" * 64


def _config(*, target: str = "apps", postgres: bool = False) -> Config:
    return Config(
        domain="example.test",
        projects=ProjectsConfig(
            entries=[
                ProjectEntry(
                    name="Status",
                    subdomain="status",
                    auth_mode="forward_auth",
                    exposure="private",
                    docker_image=PINNED_IMAGE,
                    container_port=45678,
                    placement=target,
                    health_endpoint="/ready",
                    database_service="dev-postgres" if postgres else "",
                )
            ]
        ),
    )


def test_project_compose_service_is_hardened_and_profile_scoped() -> None:
    service = project_compose_document(_config())["services"]["project-status"]

    assert service["image"] == PINNED_IMAGE
    assert service["container_name"] == "status"
    assert service["profiles"] == ["project-status"]
    assert "networks" not in service
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["ports"] == [
        {
            "target": 45678,
            "published": 45678,
            "host_ip": "${PRIVATE_IP:-127.0.0.1}",
            "protocol": "tcp",
        }
    ]
    assert service["labels"]["io.homelab.project.health-path"] == "/ready"
    assert service["labels"]["homelab.watchdog.restart-policy"] == "safe"


def test_project_database_environment_is_derived() -> None:
    service = project_compose_document(_config(postgres=True))["services"]["project-status"]

    assert service["environment"] == {
        "POSTGRES_DB": "status",
        "POSTGRES_HOST": "dev-postgres",
        "POSTGRES_PASSWORD": "${STATUS_POSTGRES_PASSWORD}",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "status",
    }
    assert service["depends_on"] == {"dev-postgres": {"condition": "service_healthy"}}


def test_project_database_uses_private_address_across_machines() -> None:
    service = project_compose_document(_config(target="media", postgres=True))["services"]["project-status"]

    assert service["environment"]["POSTGRES_HOST"] == "10.10.10.12"
    assert service["environment"]["POSTGRES_PORT"] == "5433"
    assert "depends_on" not in service


def test_project_profiles_are_enabled_only_on_the_target_node() -> None:
    cfg = _config(target="media")

    assert project_profiles_for_vm(cfg, "media") == ["project-status"]
    assert project_profiles_for_vm(cfg, "apps") == []


def test_single_node_deployment_enables_all_projects_on_infra() -> None:
    cfg = _config(target="infra")
    cfg.services.media = False
    cfg.services.cloud = False
    cfg.services.email = False

    assert project_profiles_for_vm(cfg, "infra") == ["project-status"]
    assert project_profiles_for_vm(cfg, "apps") == []


def test_project_target_keeps_its_node_in_the_deployment_plan() -> None:
    cfg = _config(target="media")
    cfg.services.media = False

    assert "media" in cfg.enabled_nodes
    assert "project-status" in _vm_service_names(cfg, "media")
