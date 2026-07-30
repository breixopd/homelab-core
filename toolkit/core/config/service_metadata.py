"""Read service metadata from service.yaml at runtime.

Flat service plugins are the canonical service definition. This module gives
watchdog resource sizing, graph enrichment, and maintenance a shared view of
``toolkit/services/<name>/service.yaml``.

The metadata keys consumed by the framework are:
  - icon: ☁️
  - restart_policy: safe | careful | never
  - depends_on: [postgres, redis]
  - memory_tier: heavy | medium | light
  - memory_floor_mb: minimum safe container memory
  - cpu_floor: minimum safe container CPU allocation
  - service_endpoint.container_port: TCP port used by consumers to verify connectivity
  - oidc: {client_id, secret_env_var}

This module caches the loaded metadata so repeated calls are fast.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def _load_all_services() -> dict[str, dict]:
    """Load validated metadata from the canonical manifest catalog."""
    from toolkit.core.manifest.catalog import load_service_catalog

    return {manifest.name: manifest.model_dump(mode="python") for manifest in load_service_catalog().manifests}


def get_service_restart_policy(service: str) -> str:
    """Return 'safe', 'careful', or 'never' for a service. Default: 'careful'."""
    svc = _service_manifest(service)
    return svc.get("restart_policy", "careful")


def get_service_depends_on(service: str) -> list[str]:
    """Return list of service dependencies for a service."""
    svc = _service_manifest(service)
    return svc.get("depends_on", [])


def get_service_memory_tier(service: str) -> str:
    """Return 'heavy', 'medium', or 'light'. Default: 'medium'."""
    svc = _service_manifest(service)
    return svc.get("memory_tier", "medium")


@lru_cache(maxsize=1)
def _runtime_service_owners() -> dict[str, str]:
    """Map every Compose runtime service to its owning service manifest."""
    from toolkit import services as services_package

    services_root = Path(services_package.__file__).parent
    owners: dict[str, str] = {}
    for owner in _load_all_services():
        compose_path = services_root / owner / "compose.yaml"
        if not compose_path.is_file():
            continue
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        for runtime_name in document.get("services") or {}:
            owners.setdefault(runtime_name, owner)
    return owners


def _service_manifest(service: str) -> dict:
    owner = service if service in _load_all_services() else _runtime_service_owners().get(service, service)
    return _load_all_services().get(owner, {})


def get_service_resource_requirements(service: str) -> tuple[int, float]:
    """Return manifest-owned memory/CPU floors for a logical or runtime service."""
    manifest = _service_manifest(service)
    runtime = (manifest.get("runtimes") or {}).get(service) or {}
    return (
        int(runtime.get("memory_floor_mb") or manifest.get("memory_floor_mb", 128)),
        float(runtime.get("cpu_floor") or manifest.get("cpu_floor", 0.1)),
    )


def get_service_runtime_mode(service: str) -> str:
    """Return the declared runtime mode for a logical or Compose service."""
    manifest = _service_manifest(service)
    runtime = (manifest.get("runtimes") or {}).get(service)
    if isinstance(runtime, dict):
        return str(runtime.get("mode", "daemon"))
    return "daemon"


def get_service_resource_policy(service: str) -> tuple[bool, int, float]:
    """Return statefulness and resource floors for a logical or runtime service."""
    manifest = _service_manifest(service)
    memory_floor_mb, cpu_floor = get_service_resource_requirements(service)
    return (
        bool(manifest.get("stateful", False)),
        memory_floor_mb,
        cpu_floor,
    )


def managed_runtime_service_names() -> set[str]:
    """Return logical and Compose service names owned by the manifest catalog."""
    return set(_load_all_services()) | set(_runtime_service_owners())


def get_service_icon(service: str) -> str:
    """Return the emoji icon for a service. Default: 🔧."""
    svc = _service_manifest(service)
    return svc.get("icon", "🔧")


def get_service_oidc_config(service: str) -> dict | None:
    """Return OIDC client config for a service, or None if no OIDC."""
    svc = _service_manifest(service)
    return svc.get("oidc")


def all_services_with_oidc() -> dict[str, dict]:
    """Return {service_name: oidc_config} for all services with OIDC configured."""
    out: dict[str, dict] = {}
    for name, svc in _load_all_services().items():
        oidc = svc.get("oidc")
        if oidc:
            out[name] = oidc
    return out


def safe_to_restart_services() -> set[str]:
    """Return all services with restart_policy=safe."""
    return {name for name, svc in _load_all_services().items() if svc.get("restart_policy") == "safe"}


def careful_restart_services() -> set[str]:
    """Return all services with restart_policy=careful or never (need checking before restart)."""
    return {name for name, svc in _load_all_services().items() if svc.get("restart_policy") in ("careful", "never")}


def never_restart_services() -> set[str]:
    """Return all services with restart_policy=never."""
    return {name for name, svc in _load_all_services().items() if svc.get("restart_policy") == "never"}


def dependency_map() -> dict[str, list[str]]:
    """Return {service: [depends_on...]} for all services."""
    return {name: svc.get("depends_on", []) for name, svc in _load_all_services().items() if svc.get("depends_on")}


def service_endpoint_ports() -> dict[str, int]:
    """Return manifest-owned internal TCP ports for service integrations."""
    ports: dict[str, int] = {}
    for name, service in _load_all_services().items():
        value = (service.get("service_endpoint") or {}).get("container_port")
        if value is not None:
            ports[name] = int(value)
    return ports
