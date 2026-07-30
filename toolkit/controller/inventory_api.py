"""Controller-owned service catalog and cross-node runtime inventory."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from toolkit.controller.contracts import MachineId
from toolkit.controller.read_models import (
    BookmarkGroup,
    BookmarkItem,
    ContainerInventory,
    ContainerStatus,
    FamilyServiceCard,
    FamilyServiceSection,
    ServiceCatalogEntry,
    ServiceCategorySummary,
    ServiceGraphEdge,
    ServiceGraphNode,
    ServiceRouteSummary,
    ServiceSummary,
    ServicesView,
    ServiceTopology,
)
from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.service_metadata import _load_all_services
from toolkit.core.config.storage import config_path
from toolkit.core.identity.service_groups import invite_sections_for_groups, validate_service_groups
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.routes import CompiledRoute, compile_routes, route_scope, service_is_enabled
from toolkit.core.ops.family_portal import family_portal_groups, tier_labels_for_groups
from toolkit.core.ops.portal_bookmarks import portal_bookmark_groups
from toolkit.core.registry.service_graph import ServiceGraph
from toolkit.services import get_service_plugin

logger = logging.getLogger(__name__)

_DOCKER_PS = 'docker ps -a --format "{{json .}}"'
_MAX_DOCKER_OUTPUT = 2 * 1024 * 1024
_MAX_STATE_BYTES = 2 * 1024 * 1024
# Inventory is controller request-path work. Keep the fan-out bounded even if
# a user configures a large fleet, while allowing the normal small fleet to be
# collected concurrently instead of paying one SSH timeout per node.
_MAX_INVENTORY_WORKERS = 4
_SUCCESSFUL_EXIT = re.compile(r"^Exited \(0\)(?:\s|$)")


class InventoryRequestError(RuntimeError):
    pass


def as_node_name(value: str) -> MachineId:
    from toolkit.core.machines.models import validate_machine_id

    return validate_machine_id(value)


def serialize_bookmarks(groups) -> list[BookmarkGroup]:
    return [
        BookmarkGroup(
            name=group.name,
            items=[
                BookmarkItem(title=item.title, href=item.href, description=item.description) for item in group.items
            ],
        )
        for group in groups
    ]


def read_services_view(root: Path, *, family: bool, groups: list[str]) -> ServicesView:
    root = root.resolve()
    cfg = load_config(config_path(root))
    if family:
        try:
            selected_groups = validate_service_groups(groups)
        except ValueError as exc:
            raise InventoryRequestError("invalid service access group") from exc
        sections = invite_sections_for_groups(cfg, selected_groups)
        return ServicesView(
            domain=cfg.domain,
            categories=[],
            bookmark_groups=serialize_bookmarks(family_portal_groups(cfg, selected_groups)),
            family_sections=[
                FamilyServiceSection(
                    name=name,
                    cards=[
                        FamilyServiceCard(
                            label=card.label,
                            url=card.url,
                            blurb=card.blurb,
                            sign_in=card.sign_in,
                        )
                        for card in cards
                    ],
                )
                for name, cards in sections
            ],
            tier_labels=tier_labels_for_groups(selected_groups),
        )

    catalog = load_service_catalog()
    routes_by_service: dict[str, list[CompiledRoute]] = defaultdict(list)
    for route in compile_routes(cfg, catalog):
        routes_by_service[route.service].append(route)

    grouped: dict[tuple[str, MachineId], list[ServiceSummary]] = {}
    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        from toolkit.core.manifest.placement import manifest_node

        node = as_node_name(manifest_node(cfg, manifest))
        key = (manifest.category, node)
        grouped.setdefault(key, []).append(
            ServiceSummary(
                name=manifest.name,
                label=manifest.label,
                description=manifest.description,
                routes=[
                    ServiceRouteSummary(
                        url=f"https://{route.host}",
                        exposure=route.exposure,
                        auth_mode=route.auth.mode,
                        scope=route_scope(route),
                    )
                    for route in routes_by_service[manifest.name]
                ],
                node=node,
                is_manageable=get_service_plugin(manifest.name) is not None,
            )
        )
    categories = [
        ServiceCategorySummary(name=category.replace("-", " ").title(), node=node, services=services)
        for (category, node), services in grouped.items()
    ]
    return ServicesView(
        domain=cfg.domain,
        categories=categories,
        bookmark_groups=serialize_bookmarks(portal_bookmark_groups(cfg)),
        family_sections=[],
        tier_labels=[],
    )


def _docker_ps(cfg: Config, root: Path, node: MachineId) -> tuple[bool, str]:
    if cfg.proxmox.provision_machines:
        code, output, _error = ssh_run_on_vm(
            cfg,
            cfg.node_ip(node),
            _DOCKER_PS,
            root=root,
            timeout=20,
            retries=1,
        )
        return code == 0, output[:_MAX_DOCKER_OUTPUT]
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return result.returncode == 0, result.stdout[:_MAX_DOCKER_OUTPUT]


def _health(row: dict[str, Any]) -> Literal["healthy", "unhealthy", "starting", "none"]:
    health = str(row.get("Health", "")).lower()
    if health == "healthy":
        return "healthy"
    if health == "unhealthy":
        return "unhealthy"
    if health == "starting":
        return "starting"
    status = str(row.get("Status", "")).lower()
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(healthy)" in status:
        return "healthy"
    if "(starting)" in status:
        return "starting"
    return "none"


def read_container_inventory(root: Path) -> ContainerInventory:
    root = root.resolve()
    cfg = load_config(config_path(root))
    completed_runtimes = {
        runtime_name
        for manifest in load_service_catalog().manifests
        if service_is_enabled(cfg, manifest)
        for runtime_name, runtime in manifest.runtimes.items()
        if runtime.mode == "oneshot"
    }
    containers: list[ContainerStatus] = []
    unavailable: list[MachineId] = []
    configured_nodes = cfg.enabled_nodes if cfg.proxmox.provision_machines else [cfg.control_node]
    nodes = [as_node_name(configured_node) for configured_node in configured_nodes]
    # Futures are consumed in configured order, not completion order. This
    # keeps unavailable-node reporting and error propagation deterministic.
    # The context manager always joins workers, so no request can leak an
    # executor thread into later requests or process shutdown.
    with ThreadPoolExecutor(max_workers=min(_MAX_INVENTORY_WORKERS, len(nodes) or 1)) as executor:
        results = [executor.submit(_docker_ps, cfg, root, node) for node in nodes]
        for node, result in zip(nodes, results, strict=True):
            ok, output = result.result()
            if len(containers) >= 512:
                continue
            if not ok:
                unavailable.append(node)
                continue
            for line in output.splitlines()[:512]:
                if len(containers) >= 512:
                    break
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("Names", ""))
                    status = str(row.get("Status", ""))[:500]
                    containers.append(
                        ContainerStatus(
                            name=name,
                            node=node,
                            status=status,
                            state=str(row.get("State", ""))[:50],
                            image=str(row.get("Image", ""))[:500],
                            health=_health(row),
                            completed=name in completed_runtimes and bool(_SUCCESSFUL_EXIT.match(status)),
                        )
                    )
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Ignored invalid Docker inventory row on node %s", node)
    containers.sort(key=lambda item: (item.node, item.name))
    return ContainerInventory(
        is_available=bool(containers) or len(unavailable) < len(configured_nodes),
        unavailable_nodes=unavailable,
        containers=containers,
    )


def _load_graph(root: Path) -> ServiceGraph:
    compose_path = root / "docker-compose.yml"
    if not compose_path.is_file() or compose_path.is_symlink():
        return ServiceGraph(nodes={})
    try:
        return ServiceGraph.from_compose(compose_path)
    except Exception:
        logger.warning("Could not build deployed service graph", exc_info=True)
        return ServiceGraph(nodes={})


def _load_watchdog_state(root: Path) -> dict[str, Any]:
    from toolkit.core.state.paths import watchdog_state_path

    path = watchdog_state_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        raw = os.read(descriptor, _MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_STATE_BYTES:
        return {}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _service_health(service: str, watchdog_state: dict[str, Any]) -> Literal["healthy", "warning", "critical"]:
    notify_state = watchdog_state.get("notify_state", {})
    if not isinstance(notify_state, dict):
        return "healthy"
    result: Literal["healthy", "warning", "critical"] = "healthy"
    for key, entry in notify_state.items():
        if not isinstance(key, str) or not key.startswith(f"{service}|") or not isinstance(entry, dict):
            continue
        if bool(entry.get("terminal")) or entry.get("severity") == "critical":
            return "critical"
        if entry.get("severity") == "warning":
            result = "warning"
    return result


def read_service_topology(root: Path) -> ServiceTopology:
    from toolkit.core.manifest.placement import service_node_map

    root = root.resolve()
    graph = _load_graph(root)
    metadata = _load_all_services()
    cfg = load_config(config_path(root)) if config_path(root).is_file() else Config()
    placements = service_node_map(cfg, load_service_catalog())
    watchdog = _load_watchdog_state(root)
    inventory = read_container_inventory(root)
    runtime = {container.name: container for container in inventory.containers}
    names = graph.service_names() or sorted(metadata)
    nodes = [
        ServiceGraphNode(
            name=name,
            health=_service_health(name, watchdog),
            image=graph.nodes[name].image if name in graph.nodes else "",
            node=placements.get(name, ""),
            tier=str((metadata.get(name) or {}).get("memory_tier", "medium")),
            category=str((metadata.get(name) or {}).get("category", "")),
            icon=str((metadata.get(name) or {}).get("icon", "")),
        )
        for name in names
    ]
    edges = [
        ServiceGraphEdge(source=consumer, target=dependency)
        for consumer, dependencies in graph.dependency_map().items()
        for dependency in dependencies
    ]
    catalog = []
    for name in names:
        meta = metadata.get(name) or {}
        container = runtime.get(name)
        catalog.append(
            ServiceCatalogEntry(
                name=name,
                label=str(meta.get("label") or name),
                description=str(meta.get("description", "")),
                node=placements.get(name, ""),
                tier=str(meta.get("memory_tier", "medium")),
                category=str(meta.get("category", "")),
                icon=str(meta.get("icon", "")),
                restart_policy=str(meta.get("restart_policy", "careful")),
                image=container.image if container else "",
                state=container.state if container else "",
                health=container.health if container else "unknown",
            )
        )
    return ServiceTopology(nodes=nodes, edges=edges, catalog=catalog)
