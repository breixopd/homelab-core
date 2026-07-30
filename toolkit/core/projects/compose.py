"""Compose projection for digest-pinned custom projects."""

from __future__ import annotations

from typing import Any

from toolkit.core.config.config import Config, ProjectEntry
from toolkit.core.projects.database import (
    project_database_provider,
    project_postgres_database,
    project_postgres_user,
)
from toolkit.core.projects.placement import project_node
from toolkit.core.projects.secrets import project_database_secret_name


def project_service_name(project: ProjectEntry) -> str:
    return f"project-{project.subdomain}"


def project_profiles_for_vm(cfg: Config, vm: str) -> list[str]:
    if not cfg.is_multi_node:
        if vm != cfg.control_node:
            return []
        return sorted(project_service_name(project) for project in cfg.projects.entries)
    return sorted(project_service_name(project) for project in cfg.projects.entries if project_node(cfg, project) == vm)


def _database_environment(cfg: Config, project: ProjectEntry) -> dict[str, str]:
    provider = project_database_provider(cfg, project)
    if provider is None:
        return {}
    contract = provider.database_provider
    endpoint = provider.service_endpoint
    if contract is None or endpoint is None:
        raise ValueError(f"database provider {provider.name!r} has an incomplete manifest contract")
    from toolkit.core.manifest.placement import service_address, service_is_local

    password_name = project_database_secret_name(project.subdomain)
    local = service_is_local(cfg, project_node(cfg, project), provider.name)
    if not local and endpoint.published_port is None:
        raise ValueError(f"database service {provider.name!r} does not publish a cross-node port")
    compose_service = endpoint.compose_service or provider.name
    return {
        "POSTGRES_DB": project_postgres_database(project),
        "POSTGRES_HOST": compose_service if local else service_address(cfg, provider.name),
        "POSTGRES_PASSWORD": f"${{{password_name}}}",
        "POSTGRES_PORT": str(endpoint.container_port if local else endpoint.published_port),
        "POSTGRES_USER": project_postgres_user(project),
    }


def _project_service(cfg: Config, project: ProjectEntry) -> dict[str, Any]:
    service_name = project_service_name(project)
    service: dict[str, Any] = {
        "image": project.docker_image,
        "container_name": project.subdomain,
        "profiles": [service_name],
        "restart": "unless-stopped",
        "init": True,
        "read_only": project.read_only,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m"],
        "ports": [
            {
                "target": project.container_port,
                "published": project.container_port,
                "host_ip": "${PRIVATE_IP:-127.0.0.1}",
                "protocol": "tcp",
            }
        ],
        "labels": {
            "io.homelab.managed": "true",
            "io.homelab.project": project.subdomain,
            "io.homelab.project.health-path": project.health_endpoint,
            "io.homelab.project.node": project_node(cfg, project),
            "homelab.watchdog.restart-policy": "safe",
        },
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        },
        "mem_limit": "512m",
        "cpus": 1.0,
        "pids_limit": 256,
    }
    environment = _database_environment(cfg, project)
    if environment:
        service["environment"] = environment
        provider = project_database_provider(cfg, project)
        if provider is None or provider.database_provider is None or provider.service_endpoint is None:
            raise ValueError(f"project {project.subdomain!r} database provider contract changed during compilation")
        compose_service = provider.service_endpoint.compose_service or provider.name
        if environment["POSTGRES_HOST"] == compose_service:
            service["depends_on"] = {compose_service: {"condition": "service_healthy"}}
    return service


def project_compose_document(cfg: Config) -> dict[str, dict[str, Any]]:
    """Return the deterministic Compose fragment for all configured projects."""
    return {
        "services": {
            project_service_name(project): _project_service(cfg, project)
            for project in sorted(cfg.projects.entries, key=lambda item: item.subdomain)
        }
    }
