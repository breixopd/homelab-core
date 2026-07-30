"""Post-start service hook lifecycle helpers used by deployment workflows."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.compose.docker import DockerCompose, compose_for_root
from toolkit.core.compose.registry import dependency_sort, enabled_categories, load_all
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def run_post_start_hooks(
    cfg: Config,
    root: Path,
    vm: str | None = None,
    *,
    on_progress: Callable[[str], None] | None = None,
    load_all_fn: Callable[[], None] = load_all,
    enabled_categories_fn: Callable[[Config], list] = enabled_categories,
    compose_for_root_fn: Callable[..., DockerCompose | None] = compose_for_root,
    dependency_sort_fn: Callable[[list], list] = dependency_sort,
) -> dict[str, list[str]]:
    """Run enabled service setup plugins on the given node in dependency order."""
    load_all_fn()
    results: dict[str, list[str]] = {}
    vm = vm or os.environ.get("HOMELAB_NODE") or None

    cats = enabled_categories_fn(cfg)
    if not cats:
        return results
    running_services: set[str] | None = None
    try:
        dc_root = compose_for_root_fn(cfg, root, vm=vm)
        if dc_root is None:
            results[vm or "local"] = ["No generated Compose application - skipped service setup."]
            return results
        containers = dc_root.ps(timeout=30)
        running = [c for c in containers if c.state == "running"]
        if not running:
            results[vm or "local"] = ["No running containers - skipped service setup to avoid unreachable APIs."]
            return results
        running_services = {c.service for c in running}
    except (OSError, ValueError, RuntimeError) as exc:
        results[vm or "local"] = [f"Plugin error: Docker runtime inspection failed: {exc}"]
        return results

    secrets: dict[str, str] = {}
    try:
        from toolkit.core.secrets.secrets import load_runtime_secrets

        secrets = load_runtime_secrets(root, role=vm or cfg.control_node)
    except ImportError:
        pass

    for cat in dependency_sort_fn(cats):
        plugins: dict = {}
        try:
            from toolkit.services import load_service_plugins

            plugins = load_service_plugins(cat.name)
        except ImportError:
            pass

        for service, plugin in plugins.items():
            if not plugin.is_enabled(cfg):
                continue
            if vm and cfg.is_multi_node and plugin.runtime_node(cfg) != vm:
                continue
            if running_services is not None and plugin._yaml_data.get("runtime", "container") != "embedded":
                owned_runtimes = {service, *plugin._yaml_data.get("runtimes", {})}
                if owned_runtimes.isdisjoint(running_services):
                    continue
            started = time.monotonic()
            if on_progress:
                on_progress(f"  → {service}: applying service setup")
            try:
                plugin_logs = plugin.post_start(cfg, secrets, root=root)
                if plugin_logs:
                    results[f"{cat.name}::{service}"] = plugin_logs
                if on_progress:
                    on_progress(f"  ✓ {service}: setup complete ({time.monotonic() - started:.1f}s)")
            except Exception as exc:
                results[f"{cat.name}::{service}"] = [f"Plugin error: {exc}"]
                if on_progress:
                    on_progress(f"  ✗ {service}: setup failed ({time.monotonic() - started:.1f}s)")
    return results


def run_post_start_hooks_remote(
    cfg: Config,
    root: Path,
    vm: str,
    *,
    run_post_start_hooks_fn: Callable[..., dict[str, list[str]]] = run_post_start_hooks,
) -> tuple[dict[str, list[str]], bool]:
    """Run hooks on a guest LXC via SSH when multi-VM; otherwise locally."""
    from toolkit.core.deploy.hook_audit import audit_hook_results

    if cfg.is_multi_node and cfg.proxmox.provision_machines:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        cmd = (
            f"export HOMELAB_NODE={vm} HOMELAB_ROOT={DEFAULT_HOMELAB_ROOT} && "
            f"cd {DEFAULT_HOMELAB_ROOT} && .venv/bin/homelab-toolkit "
            f"--root {DEFAULT_HOMELAB_ROOT} deploy hooks --node {vm}"
        )
        rc, out, err = ssh_run_on_vm(cfg, cfg.node_ip(vm), cmd, root=root.resolve(), timeout=900)
        lines = [ln.strip() for ln in (out + err).splitlines() if ln.strip()]
        audit = audit_hook_results({vm: lines}, vm_hint=vm)
        if lines:
            return {vm: lines}, rc == 0 and audit.passed
        return {}, rc == 0
    results = run_post_start_hooks_fn(cfg, root, vm=vm)
    audit = audit_hook_results(results, vm_hint=vm or "local")
    return results, audit.passed


def reconcile_runtime_credentials(cfg: Config, root: Path, vm: str) -> list[str]:
    """Run every enabled plugin's runtime credential reconciliation for one guest."""
    from toolkit.services import enabled_service_plugins

    logs: list[str] = []
    for _, plugin in enabled_service_plugins(cfg):
        if plugin.runtime_node(cfg) != vm:
            continue
        logs.extend(plugin.reconcile_runtime_credentials(cfg, root))
    return logs


def wait_for_healthy(dc: DockerCompose, service: str, timeout: int = 60) -> bool:
    """Wait for a service container to be healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        containers = dc.ps()
        for c in containers:
            if c.service == service and c.health == "healthy":
                return True
        time.sleep(2)
    return False
