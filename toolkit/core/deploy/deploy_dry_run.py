"""Offline deployment plan rendering."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult
    from toolkit.core.infra.host_capacity import HostCapacity, MachineResourcePlan


async def run_dry_run_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    targets: tuple[str, ...] | None = None,
    workflow_step_labels_fn: Callable[[Config], dict[str, str]],
    essential_guard_fn: Callable[[Config], None],
    load_all_fn: Callable[[], None],
    enabled_plugin_runtimes_fn: Callable[..., list],
    build_machine_resource_plans_fn: Callable[..., dict[str, MachineResourcePlan]],
    format_resource_plan_fn: Callable[[dict[str, MachineResourcePlan]], str],
    configured_capacity_estimate_fn: Callable[[Config], HostCapacity | None],
) -> DeployWorkflowResult:
    """Show resource allocation plan and service list without making changes."""
    from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult

    root = root.resolve()
    step_status = {step: "pending" for step in workflow_step_labels_fn(cfg)}
    target_vms = list(targets or tuple(cfg.enabled_nodes))
    essential_guard_fn(cfg)

    on_log("=" * 60)
    on_log("  HOMELAB TOOLKIT — DRY RUN DEPLOYMENT SUMMARY")
    on_log("=" * 60)
    on_log("\n📊 Host Capacity:")
    cap = configured_capacity_estimate_fn(cfg)
    if cap is None:
        on_log("  Source: offline estimate unavailable")
        on_log("  Set host_capacity.cpu_cores and host_capacity.mem_total_mb for an allocation comparison.")
    else:
        on_log(f"  Source: {cap.source}")
        on_log(f"  CPU cores: {cap.cpu_cores}")
        on_log(f"  RAM: {cap.mem_total_mb} MB")
        on_log("  Load (1m): not queried in dry-run")
        on_log(f"  Wave timeout: {cap.wave_timeout_s}s")
        on_log(f"  Inter-wave sleep: {cap.inter_wave_sleep_s}s")

    from toolkit.core.projects.compose import project_profiles_for_vm

    load_all_fn()
    service_counts: dict[str, int] = {}
    vm_services: dict[str, list[str]] = {}
    for vm in target_vms:
        svc_names = [
            runtime for _category, _plugin, runtimes in enabled_plugin_runtimes_fn(cfg, vm) for runtime in runtimes
        ]
        svc_names.extend(project_profiles_for_vm(cfg, vm))
        service_counts[vm] = len(svc_names)
        vm_services[vm] = svc_names

    on_log("\n📦 LXC Resource Allocation:")
    try:
        plans = build_machine_resource_plans_fn(cfg, service_counts)
        on_log(format_resource_plan_fn(plans))
        allocated_cores = sum(plan.cores for plan in plans.values())
        allocated_memory = sum(plan.memory_mb for plan in plans.values())
        if cap is not None and (allocated_cores > cap.cpu_cores or allocated_memory > cap.mem_total_mb):
            on_log(
                "  WARNING: declared machine resources exceed configured host capacity "
                f"({allocated_cores}/{cap.cpu_cores} CPU, {allocated_memory}/{cap.mem_total_mb} MB RAM)"
            )
    except Exception as exc:
        on_log(f"  (resource calculation skipped: {exc})")
        plans = {}

    on_log("\n🛠️  Services per LXC:")
    for vm in target_vms:
        svcs = vm_services.get(vm, [])
        on_log(f"  [{vm}] ({len(svcs)} services)")
        for svc in svcs:
            on_log(f"    • {svc}")
    on_log("\n🔧 Deployment Pipeline (dry-run, all steps shown):")
    labels = workflow_step_labels_fn(cfg)
    for step_id in labels:
        on_log(f"  ○ {labels[step_id]}")
    on_log("\n" + "-" * 60)
    on_log("\n⚙️  Configuration Summary:")
    on_log(f"  Domain: {cfg.domain}")
    on_log(f"  Email: {cfg.email}")
    on_log(f"  Timezone: {cfg.timezone}")
    on_log(f"  Services enabled: {', '.join(cfg.enabled_categories)}")
    on_log(f"  Nodes: {', '.join(target_vms)}")
    on_log(f"  Provision machines: {cfg.proxmox.provision_machines}")
    on_log(f"  Multi-node mode: {cfg.is_multi_node}")
    total_services = sum(service_counts.values())
    est_minutes = max(10, total_services * 2)
    on_log(f"\n⏱️  Estimated time: ~{est_minutes} minutes ({total_services} services)")
    on_log("=" * 60)
    on_log("\n✅ Dry-run complete. No local or remote changes were made.")
    return DeployWorkflowResult(
        success=True,
        message="Dry-run completed — no changes made",
        notification_type="positive",
        step_status=step_status,
    )
