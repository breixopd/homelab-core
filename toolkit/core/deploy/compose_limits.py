"""Generate per-VM compose.limits.yml overlay (DEP-002) from capacity plans."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config
from toolkit.core.config.storage import env_path
from toolkit.core.generate.resources import calculate_service_limits
from toolkit.core.infra.host_capacity import build_machine_resource_plans


def _compose_service_names(root: Path, cfg: Config, vm: str) -> set[str]:
    from toolkit.core.compose.docker import deployment_compose_path

    compose = deployment_compose_path(cfg, root, vm)
    if not compose.is_file():
        return set()
    names: set[str] = set()
    for line in compose.read_text().splitlines():
        m = re.match(r"^  ([a-zA-Z0-9][a-zA-Z0-9_.-]*):\s*$", line)
        if m and not line.strip().startswith("#"):
            names.add(m.group(1))
    return names


def _vm_service_names(cfg: Config, vm: str) -> list[str]:
    load_all()
    names: list[str] = []
    for cat in enabled_categories(cfg):
        if cat.runtime_node(cfg) != vm:
            continue
        for svc in cat.services(cfg):
            names.append(svc.name)
    from toolkit.core.projects.compose import project_profiles_for_vm

    names.extend(project_profiles_for_vm(cfg, vm))
    return names


def write_compose_limits(
    cfg: Config,
    vm: str,
    root: Path | None = None,
) -> Path | None:
    """Write generated/{vm}/compose.limits.yml capped to LXC allocation."""
    root = (root or Path.cwd()).resolve()
    out = env_path(vm, root).parent / "compose.limits.yml"
    compose_names = _compose_service_names(root, cfg, vm)
    if not compose_names:
        return None

    enabled = list(cfg.enabled_nodes)
    service_counts = {v: len(_compose_service_names(root, cfg, v) or set(_vm_service_names(cfg, v))) for v in enabled}
    plans = build_machine_resource_plans(cfg, service_counts)
    plan = plans.get(vm)
    if not plan:
        return None

    from toolkit.core.config.service_metadata import managed_runtime_service_names

    managed_names = managed_runtime_service_names() | set(_vm_service_names(cfg, vm))
    svc_names = sorted(compose_names & managed_names)
    limits = calculate_service_limits(plan.memory_mb, plan.cores, svc_names)
    max_cpus = max(round(plan.cores * 0.95, 2), 0.1)
    for name, spec in limits.items():
        cpus = float(spec["cpus"])
        if cpus > max_cpus:
            limits[name] = {**spec, "cpus": str(max_cpus)}
    for name, override in cfg.machines[vm].resource_limits.items():
        if name in limits and name in compose_names:
            limits[name] = {
                "mem_limit": f"{override.memory_mb}m",
                "cpus": str(override.cpus),
            }
    if not limits:
        return None

    payload = {
        "services": {
            name: {"mem_limit": spec["mem_limit"], "cpus": spec["cpus"]}
            for name, spec in limits.items()
            if name in compose_names
        }
    }
    header = "# AUTO-GENERATED — per-service CPU/RAM caps (DEP-002)\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.dump(payload, default_flow_style=False, sort_keys=False))
    return out
