"""Build the deployable Compose model from service-owned applications."""

from __future__ import annotations

import hashlib
import ipaddress
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from toolkit.core.config.config import Config
from toolkit.core.manifest.schema import NodeId

_SECTIONS = ("services", "networks", "volumes", "configs", "secrets")
_PLATFORM_SECTIONS = _SECTIONS[1:]
_HEADER = """\
# ------------------------------------------------------------------------------
# Generated deployment model
# Built from toolkit/services/*/compose.yaml and stacks/platform.yaml.
# Edit those sources, then run `homelab-toolkit generate`.
# ------------------------------------------------------------------------------
"""
_ROLE_HEADER = """\
# ------------------------------------------------------------------------------
# Generated role-scoped deployment model
# Built from service manifests, service-owned Compose applications, and platform resources.
# Edit those sources, then run `homelab-toolkit generate`.
# ------------------------------------------------------------------------------
"""


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys."""


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ValueError(f"duplicate YAML key {key!r} at line {mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _load_mapping(path: Path, *, require_services: bool = False) -> dict[str, Any]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"invalid Compose source {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"invalid Compose source {path}: expected a top-level mapping")
    if require_services and not isinstance(document.get("services"), dict):
        raise ValueError(f"{path}: expected a non-empty top-level services mapping")
    unknown = [key for key in document if key not in _SECTIONS]
    if unknown:
        raise ValueError(f"invalid Compose source {path}: unsupported top-level keys {unknown}")
    return document


def _merge_section(
    destination: dict[str, Any],
    incoming: object,
    *,
    section: str,
    source: Path | str,
    owners: dict[str, str],
) -> None:
    if incoming is None:
        return
    if not isinstance(incoming, dict):
        raise ValueError(f"{source}: top-level {section} must be a mapping")
    for name, block in incoming.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{source}: {section} names must be non-empty strings")
        if name in destination:
            raise ValueError(f"duplicate {section[:-1]} ownership for {name!r}: {owners[name]} and {source}")
        if section == "services" and not isinstance(block, dict):
            raise ValueError(f"{source}: services.{name} must be a mapping")
        if block is not None and not isinstance(block, dict):
            raise ValueError(f"{source}: {section}.{name} must be a mapping")
        destination[name] = block
        owners[name] = str(source)


def _service_applications(root: Path) -> list[Path]:
    from toolkit.services import installed_service_bundles

    services_dir = root / "toolkit" / "services"
    if not services_dir.is_dir():
        raise FileNotFoundError(f"missing service catalog {services_dir}")
    applications = [path for path in services_dir.glob("*/compose.yaml") if path.is_file()]
    if (root / "pyproject.toml").is_file():
        applications.extend(
            path
            for _name, bundle in installed_service_bundles()
            if (path := bundle.root.resolve() / "compose.yaml").is_file()
        )
    return sorted(applications)


def _network_key(prefix: str, *owners: str) -> str:
    value = "-".join((prefix, *owners))
    if len(value) <= 63:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    return f"{value[:52]}-{digest}"


def _attach_network(service: dict[str, Any], network: str, definition: object = None) -> None:
    if service.get("network_mode"):
        return
    networks = service.get("networks")
    if networks is None:
        service["networks"] = [network] if definition is None else {network: definition}
        return
    if isinstance(networks, list):
        if network not in networks:
            networks.append(network)
        return
    if isinstance(networks, dict):
        networks.setdefault(network, definition)
        return
    raise ValueError("Compose service networks must be a list or mapping")


def _assign_network_subnets(cfg: Config, networks: dict[str, Any]) -> None:
    """Allocate deterministic small bridges without consuming Docker's finite default pools."""
    pool = ipaddress.ip_network(cfg.network.container_ipv4_cidr)
    prefix = cfg.network.container_network_prefix
    slot_count = 1 << (prefix - pool.prefixlen)
    block_size = 1 << (32 - prefix)
    candidates = [
        name
        for name, raw in networks.items()
        if not (isinstance(raw, dict) and (raw.get("external") or raw.get("ipam")))
        and (not isinstance(raw, dict) or raw.get("driver", "bridge") == "bridge")
    ]
    if len(candidates) > slot_count:
        raise ValueError(
            f"container network pool {pool} provides {slot_count} /{prefix} bridges, but {len(candidates)} are required"
        )

    occupied: set[int] = set()
    for name in sorted(candidates):
        slot = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big") % slot_count
        initial_slot = slot
        while slot in occupied:
            slot = (slot + 1) % slot_count
            if slot == initial_slot:
                raise ValueError(f"container network pool {pool} is exhausted")
        occupied.add(slot)
        subnet = ipaddress.IPv4Network((int(pool.network_address) + slot * block_size, prefix))
        definition = networks[name]
        if definition is None:
            definition = {}
            networks[name] = definition
        definition.setdefault("driver", "bridge")
        definition["ipam"] = {"config": [{"subnet": str(subnet)}]}


def _compile_service_networks(
    root: Path,
    cfg: Config | None,
    services: dict[str, dict[str, Any]],
    networks: dict[str, Any],
    service_owners: dict[str, str],
) -> None:
    """Create plugin-local bridges and explicit same-node trust relationships."""
    owner_services: dict[str, set[str]] = {}
    for service_name, owner in service_owners.items():
        if service_name not in services:
            continue
        owner_services.setdefault(owner, set()).add(service_name)
        if owner in {"caddy", "prometheus"}:
            continue
        plugin_network = _network_key("plugin", owner)
        networks.setdefault(plugin_network, {"driver": "bridge"})
        _attach_network(services[service_name], plugin_network)

    if cfg is None:
        return

    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog(root)
    enabled = {
        manifest.name: manifest
        for manifest in catalog.manifests
        if service_is_enabled(cfg, manifest, catalog) and manifest.name in owner_services
    }

    def attach_link(left: str, right: str) -> None:
        if left == right or left not in enabled or right not in enabled or "caddy" in {left, right}:
            return
        if manifest_node(cfg, enabled[left]) != manifest_node(cfg, enabled[right]):
            return
        owners = tuple(sorted((left, right)))
        network = _network_key("link", *owners)
        networks.setdefault(network, {"driver": "bridge", "internal": True})
        for owner in owners:
            for service_name in sorted(owner_services[owner]):
                _attach_network(services[service_name], network)

    for manifest in enabled.values():
        for dependency in manifest.depends_on:
            attach_link(manifest.name, dependency)
        for integration in manifest.integrations:
            attach_link(manifest.name, integration.service)
        for binding in manifest.databases:
            attach_link(manifest.name, binding.provider)
        if manifest.oidc is not None:
            attach_link(manifest.name, "authelia")

    # Compose-level dependencies capture tightly coupled runtimes such as
    # Grafana data sources and the Wazuh dashboard/indexer pair.
    for service_name, service in services.items():
        service_owner = service_owners.get(service_name)
        if service_owner is None or service_owner == "caddy":
            continue
        dependencies = service.get("depends_on")
        names = (
            dependencies
            if isinstance(dependencies, list)
            else dependencies.keys()
            if isinstance(dependencies, dict)
            else ()
        )
        for dependency in names:
            dependency_owner = service_owners.get(str(dependency))
            if dependency_owner is not None:
                attach_link(service_owner, dependency_owner)

    from toolkit.core.projects.database import project_database_provider
    from toolkit.core.projects.placement import project_node

    for project in cfg.projects.entries:
        owner = f"project-{project.subdomain}"
        provider = project_database_provider(cfg, project)
        if provider is None or owner not in owner_services or provider.name not in owner_services:
            continue
        if project_node(cfg, project) != manifest_node(cfg, provider):
            continue
        network = _network_key("link", *sorted((owner, provider.name)))
        networks.setdefault(network, {"driver": "bridge", "internal": True})
        for linked_owner in (owner, provider.name):
            for service_name in sorted(owner_services[linked_owner]):
                _attach_network(services[service_name], network)

    _assign_network_subnets(cfg, networks)


def _inject_oidc_split_dns(
    root: Path,
    cfg: Config,
    service_name: str,
    services: dict[str, Any],
) -> None:
    """Keep OIDC backchannel traffic on the private ingress address."""
    if cfg.domain == "localhost":
        return
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node

    hostname = f"auth.{cfg.domain}"
    ingress = load_service_catalog(root).require_provider("ingress")
    address = cfg.node_ip(manifest_node(cfg, ingress))
    for runtime_name, runtime in services.items():
        if not isinstance(runtime, dict):
            raise ValueError(f"OIDC service {service_name!r} has an invalid runtime {runtime_name!r}")
        extra_hosts = runtime.get("extra_hosts")
        if extra_hosts is None:
            runtime["extra_hosts"] = {hostname: address}
            continue
        if isinstance(extra_hosts, dict):
            existing = extra_hosts.get(hostname)
            if existing is not None and existing != address:
                raise ValueError(
                    f"OIDC service {service_name!r} runtime {runtime_name!r} maps {hostname!r} "
                    f"to {existing!r}, expected private ingress {address!r}"
                )
            extra_hosts[hostname] = address
            continue
        if isinstance(extra_hosts, list):
            matching = [
                entry
                for entry in extra_hosts
                if isinstance(entry, str) and entry.replace("=", ":", 1).split(":", 1)[0] == hostname
            ]
            if matching:
                if matching != [f"{hostname}={address}"] and matching != [f"{hostname}:{address}"]:
                    raise ValueError(
                        f"OIDC service {service_name!r} runtime {runtime_name!r} has a conflicting "
                        f"private issuer mapping"
                    )
            else:
                extra_hosts.append(f"{hostname}={address}")
            continue
        raise ValueError(
            f"OIDC service {service_name!r} runtime {runtime_name!r} extra_hosts must be a list or mapping"
        )


def apply_release_images(document: dict[str, Any], images: dict[str, str]) -> None:
    """Apply validated digest pins to declared image-backed Compose services."""
    services = document.get("services")
    if not isinstance(services, dict):
        raise ValueError("Compose document has no services mapping")
    invalid = sorted(
        name
        for name in images
        if name not in services or not isinstance(services[name], dict) or not services[name].get("image")
    )
    if invalid:
        raise ValueError(f"release references unknown or image-less services: {', '.join(invalid)}")
    for name, image in images.items():
        services[name]["image"] = image


def apply_manifest_release_versions(root: Path, cfg: Config, document: dict[str, Any]) -> None:
    """Project service-owned release tags into a model used only for update discovery."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog(root)
    services = document.get("services")
    if not isinstance(services, dict):
        raise ValueError("Compose document has no services mapping")
    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest, catalog):
            continue
        release = manifest.image_release
        if release is None:
            continue
        service = services.get(release.compose_service)
        if not isinstance(service, dict) or not service.get("image"):
            raise ValueError(
                f"release image for {manifest.name!r} targets missing Compose service {release.compose_service!r}"
            )
        service["image"] = release.version_ref


def _compose_document(root: Path, cfg: Config | None, *, include_release: bool = True) -> dict[str, Any]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    platform_path = root / "stacks" / "platform.yaml"
    if not platform_path.is_file():
        raise FileNotFoundError(f"missing {platform_path}")
    platform = _load_mapping(platform_path)
    if "services" in platform:
        raise ValueError(f"{platform_path}: platform resources must not define services")

    merged: dict[str, dict[str, Any]] = {section: {} for section in _SECTIONS}
    owners: dict[str, dict[str, str]] = {section: {} for section in _SECTIONS}
    for section in _PLATFORM_SECTIONS:
        _merge_section(
            merged[section],
            platform.get(section),
            section=section,
            source=platform_path,
            owners=owners[section],
        )

    catalog = load_service_catalog(root) if cfg is not None else None
    service_owners: dict[str, str] = {}
    applications = _service_applications(root)
    if not applications:
        raise ValueError("no service Compose applications found in the service catalog")
    for path in applications:
        if cfg is not None:
            if catalog is None:
                raise RuntimeError("service catalog was not loaded for configuration-aware Compose assembly")
            manifest = catalog.require(path.parent.name)
            if not service_is_enabled(cfg, manifest, catalog):
                continue
        application = _load_mapping(path, require_services=True)
        services = application.get("services")
        if not isinstance(services, dict) or not services:
            raise ValueError(f"{path}: expected a non-empty top-level services mapping")
        for service_name in services:
            service_owners[service_name] = path.parent.name
        if cfg is not None and manifest.oidc is not None:
            routed_runtimes = {
                route.compose_service or route.upstream.partition(":")[0]
                for route in manifest.routes
                if route.compose_service or route.upstream
            }
            oidc_services = {name: services[name] for name in routed_runtimes if name in services}
            if not oidc_services:
                raise ValueError(f"OIDC service {manifest.name!r} has no routed Compose runtime")
            _inject_oidc_split_dns(root, cfg, manifest.name, oidc_services)
        for section in _SECTIONS:
            _merge_section(
                merged[section],
                application.get(section),
                section=section,
                source=path,
                owners=owners[section],
            )

    if cfg is not None and cfg.projects.entries:
        from toolkit.core.projects.compose import project_compose_document

        projects = project_compose_document(cfg)
        for service_name in projects.get("services", {}):
            service_owners[service_name] = service_name
        for section in _SECTIONS:
            _merge_section(
                merged[section],
                projects.get(section),
                section=section,
                source="declarative projects",
                owners=owners[section],
            )

    _compile_service_networks(root, cfg, merged["services"], merged["networks"], service_owners)

    document: dict[str, Any] = {"name": "homelab", "services": merged["services"]}
    for section in _PLATFORM_SECTIONS:
        if merged[section]:
            document[section] = merged[section]
    if cfg is not None and not cfg.is_multi_node:
        _inject_backup_mounts(root, cfg, cfg.control_node, document["services"])
    if include_release:
        from toolkit.core.ops.release_state import load_active_release

        release = load_active_release(root)
        if release is not None:
            apply_release_images(document, release.images)
    return document


def _prune_dependencies(services: dict[str, dict[str, Any]]) -> None:
    selected = set(services)
    for name, service in services.items():
        dependencies = service.get("depends_on")
        if isinstance(dependencies, list):
            retained_list = [dependency for dependency in dependencies if dependency in selected]
            if retained_list:
                service["depends_on"] = retained_list
            else:
                service.pop("depends_on", None)
        elif isinstance(dependencies, dict):
            retained_map = {dependency: value for dependency, value in dependencies.items() if dependency in selected}
            if retained_map:
                service["depends_on"] = retained_map
            else:
                service.pop("depends_on", None)

        for field in ("network_mode", "ipc", "pid"):
            value = service.get(field)
            if isinstance(value, str) and value.startswith("service:"):
                target = value.removeprefix("service:")
                if target not in selected:
                    raise ValueError(f"role model service {name!r} references omitted service {target!r} via {field}")


def _root_build_contexts(services: dict[str, dict[str, Any]]) -> None:
    """Root local build contexts at the installation directory.

    Role models live below ``generated/<role>`` while their source trees remain
    at the repository root. Compose resolves relative contexts from the model
    file, so leaving them unchanged makes every guest-side local build fail.
    """
    prefix = "${INSTALL_ROOT:-.}"
    for service in services.values():
        build = service.get("build")
        context: object
        if isinstance(build, str):
            context = build
            container: dict[str, Any] | None = None
        elif isinstance(build, dict):
            context = build.get("context")
            container = build
        else:
            continue
        if not isinstance(context, str) or not context.startswith("."):
            continue
        rooted = prefix if context == "." else f"{prefix}/{context.removeprefix('./')}"
        if container is None:
            service["build"] = rooted
        else:
            container["context"] = rooted


def _inject_backup_mounts(
    root: Path,
    cfg: Config,
    role: NodeId,
    services: dict[str, dict[str, Any]],
) -> None:
    """Mount only declared snapshot inputs into the node-local Kopia runtime."""
    if not cfg.backups.enabled:
        return
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_storage_nodes, service_node
    from toolkit.core.manifest.routes import service_is_enabled

    container_name = "kopia" if role == service_node(cfg, "kopia") else "kopia-agent"
    container = services.get(container_name)
    if container is None:
        return
    existing = container.get("volumes", [])
    if not isinstance(existing, list):
        raise ValueError(f"{container_name} volumes must be a list")

    def is_snapshot_mount(mount: object) -> bool:
        if isinstance(mount, dict):
            return str(mount.get("target", "")).startswith("/source")
        if not isinstance(mount, str):
            return False
        fields = mount.split(":")
        target = fields[-2] if fields[-1] in {"ro", "rw"} and len(fields) >= 2 else fields[-1]
        return target.startswith("/source")

    mounts: list[dict[str, Any] | str] = [mount for mount in existing if not is_snapshot_mount(mount)]
    mounts.extend(
        [
            {
                "type": "bind",
                "source": "${INSTALL_ROOT}/config.yaml",
                "target": "/source/config.yaml",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "${KOPIA_DUMPS_SOURCE}",
                "target": "/source/backup-dumps",
                "read_only": True,
            },
        ]
    )
    if role == cfg.control_node:
        mounts.append(
            {
                "type": "bind",
                "source": "${INSTALL_ROOT}/secrets.enc.yaml",
                "target": "/source/secrets.enc.yaml",
                "read_only": True,
            }
        )
    for manifest in load_service_catalog(root).manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        for asset in manifest.data_specs:
            if not asset.snapshot or role not in manifest_storage_nodes(cfg, manifest, asset.runtime_service):
                continue
            source = asset.volume
            mount_type = "volume"
            if asset.source_env is not None:
                source = f"${{{asset.source_env}}}"
                if asset.source_subpath:
                    source += f"/{asset.source_subpath}"
                mount_type = "bind"
            mounts.append(
                {
                    "type": mount_type,
                    "source": source,
                    "target": f"/source/services/{manifest.name}/{asset.name}",
                    "read_only": True,
                }
            )
    container["volumes"] = mounts


def _named_references(services: dict[str, dict[str, Any]], section: str, available: set[str]) -> set[str]:
    references: set[str] = set()
    for service in services.values():
        entries = service.get(section)
        if isinstance(entries, dict):
            references.update(name for name in entries if name in available)
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            source: object
            if isinstance(entry, str):
                source = entry.split(":", 1)[0] if section == "volumes" else entry
            elif isinstance(entry, dict):
                source = entry.get("source")
            else:
                continue
            if isinstance(source, str) and source in available:
                references.add(source)
    return references


def _role_compose_document(root: Path, cfg: Config, role: NodeId) -> dict[str, Any]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.projects.compose import project_service_name
    from toolkit.core.projects.placement import project_node

    full = _compose_document(root, cfg)
    catalog = load_service_catalog(root)
    selected_names: set[str] = set()

    for path in _service_applications(root):
        application = _load_mapping(path, require_services=True)
        application_services = application["services"]
        manifest = catalog.require(path.parent.name)
        unknown = set(manifest.runtimes) - set(application_services)
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise ValueError(f"{path.parent / 'service.yaml'} places unknown runtime services: {rendered}")
        if not service_is_enabled(cfg, manifest):
            continue
        for service_name in application_services:
            placements = manifest_runtime_nodes(cfg, manifest, service_name)
            if role in placements:
                selected_names.add(service_name)

    selected_names.update(
        project_service_name(project) for project in cfg.projects.entries if project_node(cfg, project) == role
    )
    services = {name: deepcopy(service) for name, service in full["services"].items() if name in selected_names}
    _prune_dependencies(services)
    _root_build_contexts(services)
    _inject_backup_mounts(root, cfg, role, services)

    document: dict[str, Any] = {"name": "homelab", "services": services}
    for section in _PLATFORM_SECTIONS:
        available = full.get(section, {})
        if not isinstance(available, dict):
            continue
        referenced = _named_references(services, section, set(available))
        if referenced:
            document[section] = {
                name: deepcopy(definition) for name, definition in available.items() if name in referenced
            }
    return document


def assemble_compose_text(root: Path, cfg: Config | None = None, *, include_release: bool = True) -> str:
    """Return a deterministic, flattened Compose document."""
    rendered = yaml.dump(
        _compose_document(root, cfg, include_release=include_release),
        Dumper=_NoAliasDumper,
        sort_keys=False,
        width=120,
        default_flow_style=False,
    )
    return _HEADER + rendered


def assemble_role_compose_text(root: Path, cfg: Config, role: NodeId) -> str:
    """Return the minimal, deterministic Compose model for one node role."""
    if role not in cfg.enabled_nodes:
        raise ValueError(f"node role {role!r} is not enabled")
    rendered = yaml.dump(
        _role_compose_document(root, cfg, role),
        Dumper=_NoAliasDumper,
        sort_keys=False,
        width=120,
        default_flow_style=False,
    )
    return _ROLE_HEADER + rendered


def write_assembled_compose(root: Path, cfg: Config | None = None) -> Path:
    from toolkit.core.generate.generate import _atomic_write

    output = root / "docker-compose.yml"
    _atomic_write(output, assemble_compose_text(root, cfg))
    return output


def write_role_compose_models(root: Path, cfg: Config) -> dict[str, Path]:
    """Write one least-privilege Compose model per enabled node in a multi-node deployment."""
    from toolkit.core.generate.generate import _atomic_write

    outputs: dict[str, Path] = {}
    enabled = set(cfg.enabled_nodes) if cfg.is_multi_node else set()
    for role in enabled:
        output = root / "generated" / role / "compose.yaml"
        _atomic_write(output, assemble_role_compose_text(root, cfg, role))
        outputs[role] = output
    generated_root = root / "generated"
    if generated_root.is_dir():
        for output in generated_root.glob("*/compose.yaml"):
            if output.parent.name not in enabled:
                output.unlink(missing_ok=True)
    return outputs
