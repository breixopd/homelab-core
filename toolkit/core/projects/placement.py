"""Resolve custom-project placement through the machine capability model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ProjectEntry


def project_node(config: Config, project: ProjectEntry) -> str:
    """Return the concrete enabled machine that owns a project."""
    if not config.is_multi_node:
        return config.control_node

    from toolkit.core.manifest.placement import resolve_node_selector

    try:
        return resolve_node_selector(config, project.placement)
    except ValueError as exc:
        raise ValueError(f"project {project.subdomain!r} has invalid placement: {exc}") from exc


ProjectPlacementKind = Literal["capability", "machine"]


def project_placement_options(config: Config) -> tuple[tuple[str, str, ProjectPlacementKind], ...]:
    """Return selector, concrete node, and selector kind for project forms."""
    labels: dict[str, list[str]] = {}
    for machine_id in config.enabled_nodes:
        for label in config.machines[machine_id].labels:
            labels.setdefault(label, []).append(machine_id)
    options: list[tuple[str, str, ProjectPlacementKind]] = [
        (label, nodes[0], "capability") for label, nodes in sorted(labels.items()) if len(nodes) == 1
    ]
    portable_selectors = {selector for selector, _node, _kind in options}
    options.extend(
        (machine_id, machine_id, "machine")
        for machine_id in config.enabled_nodes
        if machine_id not in portable_selectors
    )
    return tuple(options)


def default_project_placement(config: Config) -> str:
    """Use the concrete control machine when no placement is specified."""
    return config.control_node
