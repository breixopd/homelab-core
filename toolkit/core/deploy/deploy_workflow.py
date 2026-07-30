from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from toolkit.core.ansible.ansible_inventory import generated_extra_vars
from toolkit.core.ansible.ansible_ssh import resolve_tool
from toolkit.core.async_utils import run_blocking
from toolkit.core.compose.docker import DockerCompose, compose_for_root
from toolkit.core.compose.registry import dependency_sort, enabled_categories, load_all
from toolkit.core.config.config import Config, load_config
from toolkit.core.deploy.deploy import deploy_local
from toolkit.core.deploy.deploy_log import DeployProgressReporter, ProgressCallback
from toolkit.core.deploy.hook_audit import HookAuditSummary
from toolkit.core.deploy.operation_lease import (
    LeaseBusyError,
    OperationCancelledError,
    OperationLease,
)
from toolkit.core.generate.generate import generate_all, generate_configs
from toolkit.core.generate.validate import ValidationReport, validate_generated_artifacts
from toolkit.core.infra.host_capacity import (
    build_machine_resource_plans,
    configured_capacity_estimate,
    detect_host_capacity,
    format_resource_plan,
)
from toolkit.core.infra.iac_sync import sync_from_repo_root
from toolkit.core.ops.preflight import PreflightProfile, preflight_passed, run_preflight
from toolkit.core.ops.verify import format_report, verify_all, verify_remote

log = logging.getLogger(__name__)

DeployNotificationType = Literal["positive", "warning", "negative"]

# Docker build logs can exceed asyncio's default 64KiB StreamReader line limit.
_ANSIBLE_STREAM_LIMIT = 8 * 1024 * 1024
_MAX_VALIDATED_DUMP_BYTES = 64 * 1024 * 1024

# Core pipeline steps (ids used in UI/CLI status maps)
STEP_PREFLIGHT = "preflight"
STEP_PRE_DUMP = "pre_dump"
STEP_GENERATE = "generate"
STEP_INFRA = "infra"
STEP_HOOKS = "hooks"
STEP_HOOK_VERIFY = "hook_verify"
STEP_VAULTWARDEN = "vaultwarden"
STEP_VERIFY = "verify"
STEP_DNS = "dns"
STEP_QA = "qa"
STEP_CLEANUP = "cleanup"
STEP_NOTIFY = "notify"


def _run_pre_deploy_dump(cfg: Config, root: Path) -> tuple[bool, str | None]:
    """Run the configured database provider's pre-deploy dump safety gate.

    The provider is resolved before invoking its hook so a disabled or absent
    provider is explicitly treated as not applicable.  Once a provider is
    enabled, however, a missing artifact or hook error is a hard failure: the
    deploy must not continue without its recovery checkpoint.
    """
    from toolkit.core.ops.database_provider import (
        DatabaseProviderDisabledError,
        primary_database_node,
        primary_database_provider,
    )

    try:
        provider = primary_database_provider(cfg)
    except KeyError:
        # No primary database capability is configured for this deployment.
        return False, None
    except DatabaseProviderDisabledError:
        return False, None

    node = primary_database_node(cfg, provider)
    try:
        dump_path = provider.plugin.pre_deploy_database_dump(cfg, root, vm=node)
    except Exception as exc:
        raise RuntimeError(f"pre-deploy dump failed for {provider.manifest.name}") from exc
    if not dump_path:
        raise RuntimeError(f"pre-deploy dump produced no artifact for {provider.manifest.name}")
    dump_path = str(dump_path)
    if not _dump_artifact_verified(cfg, dump_path):
        raise RuntimeError(f"pre-deploy dump artifact is missing, empty, or invalid for {provider.manifest.name}")
    return True, dump_path


def _dump_artifact_verified(cfg: Config, dump_path: str) -> bool:
    """Verify local provider artifacts before allowing deployment to proceed."""
    from toolkit.core.config.roles import uses_remote_nodes

    if uses_remote_nodes(cfg):
        # Remote providers must perform this check on the owning host before
        # returning the path; the controller cannot inspect that filesystem.
        return True
    path = Path(dump_path)
    try:
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size <= 0:
            return False
        total = 0
        with gzip.open(path, "rb") as source:
            while chunk := source.read(64 * 1024):
                total += len(chunk)
                if total > _MAX_VALIDATED_DUMP_BYTES:
                    return False
        return total > 0
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def deploy_step_id(vm: str) -> str:
    return f"deploy_{vm}"


def ansible_target_limit(targets: tuple[str, ...] | None) -> str | None:
    """Map requested node roles to exact generated inventory groups."""
    if targets is None:
        return None
    from toolkit.core.machines.models import validate_machine_id

    if not targets or len(targets) != len(set(targets)):
        raise ValueError("unsupported deployment target")
    try:
        for target in targets:
            validate_machine_id(target)
    except ValueError as exc:
        raise ValueError("unsupported deployment target") from exc
    return ":".join(targets)


def select_guest_deploy_playbook(root: Path, *, skip_infra: bool) -> tuple[Path, str]:
    """Choose the Ansible playbook for a guest deploy.

    Normal deploys use ``guest-setup.yml`` when present so OS-level setup stays
    refreshed. ``--skip-infra`` redeploys use the lighter
    ``deploy-server-toolkit.yml`` playbook and never rerun full guest setup.
    """
    guest_setup = root / "automation" / "ansible" / "guest-setup.yml"
    deploy_playbook = root / "automation" / "ansible" / "playbooks" / "deploy-server-toolkit.yml"
    if skip_infra or not guest_setup.exists():
        return deploy_playbook, "deploy-server-toolkit"
    return guest_setup, "guest-setup"


async def generate_and_validate_artifacts(
    root: Path,
    cfg: Config,
    on_log: Callable[[str], None],
    *,
    targets: tuple[str, ...] | None = None,
) -> tuple[ValidationReport | None, DeployWorkflowResult | None]:
    """Generate and validate artifacts without mutating guest image state.

    Image reconciliation belongs to the deployment phase after SSH reachability
    is confirmed. Keeping it there avoids building/transferring the same local
    fallback images twice during one deploy.
    """
    await run_blocking(generate_all, root)
    await run_blocking(generate_configs, cfg, root)

    try:
        await run_blocking(sync_from_repo_root, root)
    except ValueError as exc:
        on_log(f"  ⚠ IaC sync skipped: {exc}")

    from toolkit.core.ansible.ansible_inventory import ensure_group_vars_all, parse_tofu_machine_ips, write_inventory

    try:
        await run_blocking(ensure_group_vars_all, root)
        machine_ips = await run_blocking(parse_tofu_machine_ips, root / "infrastructure")
        await run_blocking(write_inventory, root, cfg, machine_ips=machine_ips or None)
        on_log("  ✓ Ansible inventory refreshed from config.yaml")
    except (FileNotFoundError, ValueError) as exc:
        on_log(f"  ⚠ Inventory sync skipped: {exc}")

    validation = await run_blocking(validate_generated_artifacts, root)
    return validation, None


def deploy_soak_seconds() -> int:
    """Return the post-deploy soak window.

    Keep the production default conservative, while allowing CI and one-off
    maintenance runs to skip the wait explicitly.
    """
    raw = os.environ.get("HOMELAB_DEPLOY_SOAK_SECONDS", "60").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 60


def workflow_progress_percent(step_status: dict[str, str], cfg: Config) -> int:
    """Rough pipeline completion for CLI/WebUI progress bars."""
    if not step_status:
        return 0
    total = len(step_status)
    done = sum(1 for v in step_status.values() if v in ("ok", "fail", "skip"))
    running = sum(1 for v in step_status.values() if v == "running")
    pct = ((done + 0.35 * running) / total) * 100
    return min(100, max(0, int(pct)))


def workflow_step_labels(cfg: Config) -> dict[str, str]:
    labels = {
        STEP_PREFLIGHT: "Pre-flight checks",
        STEP_PRE_DUMP: "Pre-deploy Postgres dump (safety gate)",
        STEP_GENERATE: "Generate configs & .env files",
        STEP_INFRA: "Provision infrastructure (OpenTofu/Ansible)",
        STEP_HOOKS: "Run post-start hooks",
        STEP_VAULTWARDEN: "Sync passwords to Vaultwarden",
        STEP_DNS: "Sync DNS records (Cloudflare)",
        STEP_HOOK_VERIFY: "Verify hook configuration (API checks)",
        STEP_VERIFY: "Verify containers & HTTPS",
        STEP_QA: "Extended QA (images, Grafana, fleet)",
        STEP_CLEANUP: "Post-deploy cleanup (prune dangling images + stopped containers)",
        STEP_NOTIFY: "Send deploy notification",
    }
    for vm in cfg.enabled_nodes:
        labels[deploy_step_id(vm)] = f"Deploy {vm} LXC"
    return labels


async def _ensure_guest_custom_images(
    cfg: Config,
    root: Path,
    on_log: Callable[[str], None],
    *,
    vms: tuple[str, ...] | None = None,
) -> DeployWorkflowResult | None:
    """Reconcile custom images on reachable guests. Returns failure result or None."""
    if not cfg.is_multi_node:
        return None
    from toolkit.core.images.publish import sync_images_to_guests, verify_guest_images

    desired_tag = _generated_custom_image_tag(cfg, root, vms=vms)
    if desired_tag:
        on_log(f"  Using generated custom image release {desired_tag}")
    on_log("  Checking custom images on guest VMs...")
    img_ok, img_lines = await run_blocking(
        verify_guest_images,
        cfg,
        root,
        tag=desired_tag,
        vms=vms,
        on_log=on_log,
    )
    for line in img_lines:
        on_log(f"  {line}")
    if img_ok:
        return None
    on_log(f"  Guest images missing — reconciling via {cfg.images.source} source policy...")
    try:
        sync_lines = await run_blocking(
            sync_images_to_guests,
            root,
            cfg,
            tag=desired_tag,
            vms=vms,
            source=cfg.images.source,
            on_log=on_log,
        )
        for line in sync_lines:
            on_log(f"  {line}")
    except RuntimeError as exc:
        on_log(f"  ✗ Image sync failed: {exc}")
        return DeployWorkflowResult(
            success=False,
            message="Guest image sync failed",
            notification_type="negative",
            step_status={},
        )
    return None


def _generated_custom_image_tag(cfg: Config, root: Path, *, vms: tuple[str, ...] | None) -> str | None:
    """Return the single custom-image tag referenced by generated node environments."""
    from dotenv import dotenv_values

    from toolkit.core.config.storage import env_path
    from toolkit.core.images.publish import expected_images_for_node

    tags: set[str] = set()
    for vm in vms or tuple(cfg.enabled_nodes):
        values = dotenv_values(env_path(vm, root))
        for image in expected_images_for_node(cfg, vm, root):
            reference = str(values.get(image.env_var) or "")
            prefix = f"{cfg.images.registry}/{image.repository}:"
            if not reference.startswith(prefix):
                return None
            tag = reference[len(prefix) :]
            if not tag or "@" in tag:
                return None
            tags.add(tag)
    if len(tags) > 1:
        raise ValueError("generated node environments reference inconsistent custom image releases")
    return next(iter(tags), None)


async def _run_ansible_playbook_file(
    root: Path,
    inventory: Path,
    playbook: Path,
    on_log: Callable[[str], None],
    *,
    progress: DeployProgressReporter | None = None,
    limit: str | None = None,
) -> int:
    """Run one Ansible playbook; stream output to on_log. Returns exit code.

    Delegates to the shared ansible_runner so the streaming loop, cwd, and
    extra-vars conventions live in one place.
    """
    from toolkit.core.ansible.ansible_runner import run_playbook_streaming

    if not playbook.is_file():
        on_log(f"  Playbook not found: {playbook}")
        return 1
    return await run_playbook_streaming(
        root,
        playbook,
        inventory,
        on_log,
        progress=progress,
        limit=limit,
    )


def _guests_need_bootstrap(cfg: Config, root: Path, *, vms: tuple[str, ...] | None = None) -> bool:
    """True when any enabled guest lacks Docker (fresh LXCs before guest-setup)."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    for vm in vms or tuple(cfg.enabled_nodes):
        ip = cfg.node_ip(vm)
        if not ip:
            continue
        rc, _out, _err = ssh_run_on_vm(cfg, ip, "command -v docker", root=root, timeout=20, retries=2)
        if rc != 0:
            return True
    return False


def reconcile_infrastructure_secrets(root: Path) -> list[str]:
    """Persist controller-owned credentials derived from OpenTofu state."""
    import json

    from toolkit.core.secrets.secrets import extract_lxc_root_passwords, merge_secret_values

    try:
        passwords = extract_lxc_root_passwords(root)
        if not passwords:
            return []
        merge_secret_values(root, {"LXC_ROOT_PASSWORDS": json.dumps(passwords, sort_keys=True)})
        return [f"LXC credentials: saved {len(passwords)} machine passwords from OpenTofu state"]
    except Exception as exc:
        raise RuntimeError(f"LXC credential reconciliation failed: {exc}") from exc


# ── Post-start hooks (compatibility re-exports) ─────────────────


def run_post_start_hooks(
    cfg: Config,
    root: Path,
    vm: str | None = None,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, list[str]]:
    """Compatibility wrapper for the extracted hook lifecycle implementation."""
    from toolkit.core.deploy import deploy_hooks

    return deploy_hooks.run_post_start_hooks(
        cfg,
        root,
        vm,
        on_progress=on_progress,
        load_all_fn=load_all,
        enabled_categories_fn=enabled_categories,
        compose_for_root_fn=compose_for_root,
        dependency_sort_fn=dependency_sort,
    )


def run_post_start_hooks_remote(cfg: Config, root: Path, vm: str) -> tuple[dict[str, list[str]], bool]:
    """Compatibility wrapper for extracted remote hook execution."""
    from toolkit.core.deploy import deploy_hooks

    return deploy_hooks.run_post_start_hooks_remote(cfg, root, vm, run_post_start_hooks_fn=run_post_start_hooks)


def reconcile_runtime_credentials(cfg: Config, root: Path, vm: str) -> list[str]:
    """Compatibility wrapper for extracted runtime credential reconciliation."""
    from toolkit.core.deploy import deploy_hooks

    return deploy_hooks.reconcile_runtime_credentials(cfg, root, vm)


def wait_for_healthy(dc: DockerCompose, service: str, timeout: int = 60) -> bool:
    """Compatibility wrapper for extracted health waiting helper."""
    from toolkit.core.deploy import deploy_hooks

    return deploy_hooks.wait_for_healthy(dc, service, timeout)


async def _check_storage_active(root: Path) -> bool:
    """Check if the Proxmox ZFS storage defined in generated tfvars is active."""
    from toolkit.core.ansible.ansible_inventory import generated_extra_vars

    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    if not inventory.exists():
        return False
    ansible_bin = shutil.which("ansible") or shutil.which("ansible-playbook")
    if not ansible_bin:
        ansible_venv = root / ".venv" / "bin" / "ansible"
        ansible_bin = str(ansible_venv) if ansible_venv.is_file() else None
    if not ansible_bin:
        return False

    try:
        storage_id = load_config(root / "config.yaml").proxmox.lxc_storage
    except Exception:
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            ansible_bin,
            "-i",
            str(inventory),
            *generated_extra_vars(root),
            "proxmox_hosts",
            "-m",
            "shell",
            "-a",
            (
                "pvesm status --output-format json 2>/dev/null | python3 -c "
                '"import sys,json; d=json.load(sys.stdin); '
                f"print('active' if any(s.get('storage')=={storage_id!r} "
                "and s.get('status')=='active' for s in d) else 'inactive')\""
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return b"active" in stdout
    except Exception:
        return False


# ── Workflow result types ──────────────────────────────────────


@dataclass(slots=True)
class DeployWorkflowResult:
    success: bool
    message: str
    notification_type: DeployNotificationType
    step_status: dict[str, str]
    verify_results: dict | None = field(default=None)


def _init_step_status(cfg: Config, targets: tuple[str, ...] | None = None) -> dict[str, str]:
    target_nodes = frozenset(targets or cfg.enabled_nodes)
    return {
        step: "pending"
        for step in workflow_step_labels(cfg)
        if not step.startswith("deploy_") or step.removeprefix("deploy_") in target_nodes
    }


def _init_recover_step_status(cfg: Config, *, vm: str | None = None) -> dict[str, str]:
    """Initialize only phases that recovery actually executes."""
    targets = (vm,) if vm else tuple(cfg.enabled_nodes)
    active_steps = (
        STEP_PREFLIGHT,
        *(deploy_step_id(node) for node in targets),
        STEP_HOOKS,
        STEP_HOOK_VERIFY,
        STEP_VERIFY,
    )
    return dict.fromkeys(active_steps, "pending")


# Steps skipped when the deploy phase fails (deploy_{vm} already marked fail).
# Order matches the pipeline execution. STEP_QA is intentionally NOT included:
# the original early-return paths left it "pending" rather than "skip".
_POST_DEPLOY_SKIP_STEPS: tuple[str, ...] = (
    STEP_HOOKS,
    STEP_HOOK_VERIFY,
    STEP_VAULTWARDEN,
    STEP_VERIFY,
    STEP_DNS,
    STEP_NOTIFY,
)


def _skip_remaining_steps(
    set_step: Callable[[str, str], None],
) -> None:
    """Mark all post-deploy steps as skipped.

    Used on deploy-phase failures: the per-VM deploy steps are already marked
    "fail" by the caller, then this marks the remaining post-deploy pipeline
    (hooks, verify, vaultwarden, dns, notify) as "skip". ``STEP_QA`` is left
    untouched to preserve the original early-return behaviour.
    """
    for step in _POST_DEPLOY_SKIP_STEPS:
        set_step(step, "skip")


async def run_dry_run_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    targets: tuple[str, ...] | None = None,
) -> DeployWorkflowResult:
    """Show resource allocation plan and service list without making any changes."""
    from toolkit.core.deploy import deploy_dry_run
    from toolkit.core.deploy.essential_guard import assert_essential_services_enabled
    from toolkit.services import enabled_plugin_runtimes

    return await deploy_dry_run.run_dry_run_workflow(
        root,
        cfg,
        on_log=on_log,
        targets=targets,
        workflow_step_labels_fn=workflow_step_labels,
        essential_guard_fn=assert_essential_services_enabled,
        load_all_fn=load_all,
        enabled_plugin_runtimes_fn=enabled_plugin_runtimes,
        build_machine_resource_plans_fn=build_machine_resource_plans,
        format_resource_plan_fn=format_resource_plan,
        configured_capacity_estimate_fn=configured_capacity_estimate,
    )


async def run_deploy_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None = None,
    skip_infra: bool = False,
    skip_dns: bool = False,
    targets: tuple[str, ...] | None = None,
    preserve_controller: bool = False,
    operation_lease: OperationLease | None = None,
) -> DeployWorkflowResult:
    root = root.resolve()
    preflight_profile: PreflightProfile = (
        "controller" if os.environ.get("HOMELAB_NODE", "").strip() == cfg.control_node else "operator"
    )

    from toolkit.core.deploy.essential_guard import assert_essential_services_enabled

    assert_essential_services_enabled(cfg)

    target_vms = list(targets or tuple(cfg.enabled_nodes))
    ansible_target_limit(targets)
    if any(vm not in cfg.enabled_nodes for vm in target_vms):
        raise ValueError("deployment target is not enabled")
    step_status = _init_step_status(cfg, targets)
    hostname_to_node = {machine.hostname: node for node, machine in cfg.machines.items()}
    progress = DeployProgressReporter(
        on_log=on_log,
        on_progress=on_progress,
        hostname_to_node=hostname_to_node,
    )

    def set_step(step: str, status: str) -> None:
        step_status[step] = status
        try:
            on_step(step, status)
        except Exception as exc:
            log.warning("set_step callback failed: %s", exc)
        if status == "running":
            progress.set_step(workflow_step_labels(cfg).get(step, step))
        if on_progress is not None:
            payload = progress.snapshot.as_dict()
            payload["percent"] = str(workflow_progress_percent(step_status, cfg))
            on_progress(payload)

    hook_verify_ok = True
    hook_summary: str = ""
    lease = operation_lease
    owns_lease = operation_lease is None

    try:
        if lease is None:
            lease = OperationLease.acquire(root, "deploy")
        lease.assert_owns_root(root)
        lease.raise_if_cancelled()

        set_step(STEP_PREFLIGHT, "running")
        on_log("Step: Pre-flight checks (bootstrap)...")
        cap = detect_host_capacity(cfg=cfg, root=root, fast=True)
        on_log(f"  Host ({cap.source}): {cap.cpu_cores} cores, {cap.mem_total_mb}MB RAM, load {cap.load_1m:.1f}")
        if warn := cap.warning_message():
            on_log(f"  ℹ Load advisory: {warn}")
        bootstrap_items = run_preflight(
            root,
            cfg,
            bootstrap=True,
            require_provisioning_tools=not skip_infra,
            profile=preflight_profile,
        )
        for item in bootstrap_items:
            mark = "✓" if item.ok else ("ℹ" if item.id in {"load", "database_mesh"} else "✗")
            on_log(f"  {mark} {item.label}" + (f" — {item.detail}" if item.detail else ""))
        if not preflight_passed(bootstrap_items):
            set_step(STEP_PREFLIGHT, "fail")
            return DeployWorkflowResult(
                success=False,
                message="Pre-flight checks failed — fix items above",
                notification_type="negative",
                step_status=step_status,
            )
        lease.raise_if_cancelled()

        # Pre-deploy database dump — safety net before schema migrations or
        # password rotations. An enabled provider is a hard safety gate.
        set_step(STEP_PRE_DUMP, "running")
        try:
            applicable, dump_path = _run_pre_deploy_dump(cfg, root)
            if applicable:
                on_log(f"  ✓ Pre-deploy database dump: {Path(dump_path or '').name}")
                set_step(STEP_PRE_DUMP, "ok")
            else:
                on_log("  ○ Pre-deploy database dump skipped (no enabled provider)")
                set_step(STEP_PRE_DUMP, "skip")
        except Exception as exc:
            on_log(f"  ✗ Pre-deploy database dump failed — deployment stopped: {exc}")
            set_step(STEP_PRE_DUMP, "fail")
            return DeployWorkflowResult(
                success=False,
                message="Pre-deploy database dump safety gate failed",
                notification_type="negative",
                step_status=step_status,
            )
        lease.raise_if_cancelled()

        set_step(STEP_GENERATE, "running")
        on_log("\nStep: Generating configs...")

        missing_tools: list[str] = []
        if not shutil.which("docker"):
            missing_tools.append("docker (required for container management)")
        if cfg.proxmox.provision_machines:
            if not resolve_tool("ansible-playbook", root):
                missing_tools.append("ansible-playbook (required for container provisioning)")
            if not skip_infra:
                if not shutil.which("tofu"):
                    missing_tools.append("tofu / opentofu (required for container provisioning)")
                if not resolve_tool("jq", root):
                    missing_tools.append("jq (required for IaC output processing)")

        if missing_tools:
            on_log("  ✗ Missing required tools:")
            for tool in missing_tools:
                on_log(f"    • {tool}")
            set_step(STEP_GENERATE, "fail")
            return DeployWorkflowResult(
                success=False,
                message=f"Missing {len(missing_tools)} required tool(s). Install them and try again.",
                notification_type="negative",
                step_status=step_status,
            )

        validation, generate_failure = await generate_and_validate_artifacts(root, cfg, on_log, targets=targets)
        if generate_failure:
            generate_failure.step_status = step_status
            set_step(STEP_GENERATE, "fail")
            return generate_failure
        if validation is None:
            raise RuntimeError("artifact generation returned neither a validation report nor a failure")
        for check in validation.checks:
            on_log(f"  ✓ {check}")
        for skipped in validation.skipped:
            on_log(f"  ○ {skipped}")
        for warning in validation.warnings:
            on_log(f"  ⚠ {warning}")
        if validation.errors:
            for error in validation.errors:
                on_log(f"  ✗ {error}")
            set_step(STEP_GENERATE, "fail")
            return DeployWorkflowResult(
                success=False,
                message="Generated artifact validation failed",
                notification_type="negative",
                step_status=step_status,
            )

        set_step(STEP_GENERATE, "ok")
        lease.raise_if_cancelled()

        on_log("\nStep: Post-generate pre-flight...")
        items = run_preflight(
            root,
            cfg,
            bootstrap=False,
            require_provisioning_tools=not skip_infra,
            profile=preflight_profile,
        )
        for item in items:
            mark = "✓" if item.ok else ("ℹ" if item.id in {"load", "database_mesh"} else "✗")
            on_log(f"  {mark} {item.label}" + (f" — {item.detail}" if item.detail else ""))
        if not preflight_passed(items):
            set_step(STEP_PREFLIGHT, "fail")
            return DeployWorkflowResult(
                success=False,
                message="Post-generate pre-flight failed — fix items above",
                notification_type="negative",
                step_status=step_status,
            )
        set_step(STEP_PREFLIGHT, "ok")
        lease.raise_if_cancelled()

        # ── Auto-detect deploy state (idempotent) ──────────────────
        set_step(STEP_INFRA, "running")
        infra_freshly_provisioned = False
        inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
        auto_skip = skip_infra or not cfg.proxmox.provision_machines

        if not auto_skip:
            infra_dir = root / "infrastructure"
            tfstate = infra_dir / "terraform.tfstate"
            if tfstate.exists() and inventory.exists():
                # Check if LXCs actually exist (state must contain provisioned container resources)
                on_log("\nStep: Detecting existing infrastructure...")
                try:
                    import json as _json

                    import yaml as _yaml

                    state_data = _json.loads(tfstate.read_text())
                    state_resources = state_data.get("resources", [])
                    has_lxcs_in_state = any(
                        r.get("type") == "proxmox_virtual_environment_container"
                        and any(inst.get("attributes", {}).get("id") for inst in r.get("instances", []))
                        for r in state_resources
                    )
                    inv_data = _yaml.safe_load(inventory.read_text()) or {}
                    children = inv_data.get("all", {}).get("children", {})
                    guest_groups = children.get("guest_hosts", {}).get("children", {})
                    has_guests = any(children.get(group, {}).get("hosts") for group in guest_groups)
                    if has_lxcs_in_state and has_guests:
                        on_log("  Existing LXCs detected — skipping infrastructure provisioning")
                        on_log("  Running recover/recompose on existing guests...")
                        step_status[STEP_INFRA] = "ok"
                        set_step(STEP_INFRA, "ok")
                        auto_skip = True
                    else:
                        on_log("  No provisioned LXCs found — will provision fresh")
                except Exception:
                    on_log("  Could not inspect state, provisioning fresh...")
            else:
                on_log("\nStep: No existing infrastructure detected — fresh deploy")

        if auto_skip or skip_infra or not cfg.proxmox.provision_machines:
            on_log("\nStep: Using existing containers; skipping Proxmox provisioning.")
            set_step(STEP_INFRA, "skip" if skip_infra else "ok")
        else:
            on_log("\nStep: Provisioning infrastructure (containers)...")
            from toolkit.core.ansible.ansible_inventory import parse_tofu_machine_ips, write_inventory

            ansible_dir = root / "automation" / "ansible"
            infra_dir = root / "infrastructure"
            inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
            ansible_playbook = resolve_tool("ansible-playbook", root) or "ansible-playbook"
            tofu_bin = resolve_tool("tofu", root) or "tofu"

            try:
                # 1. Host setup (network, ZFS, templates)
                on_log("  Running host-setup (network, ZFS, kernel modules, templates)...")
                host_cmd = [
                    ansible_playbook,
                    "-i",
                    str(inventory),
                    *generated_extra_vars(root),
                    str(ansible_dir / "host-setup.yml"),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *host_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    limit=10 * 1024 * 1024,
                )
                # Stream ansible output line-by-line so the deploy log shows progress
                if proc.stdout is None:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError("host setup output pipe was not created")
                output_lines: list[str] = []
                async for raw in proc.stdout:
                    decoded = raw.decode(errors="replace").rstrip("\n")
                    output_lines.append(decoded)
                    on_log(f"    {decoded}")
                await asyncio.wait_for(proc.wait(), timeout=600)
                lease.raise_if_cancelled()
                if proc.returncode != 0:
                    on_log(f"  host-setup failed (exit {proc.returncode})")
                    set_step(STEP_INFRA, "fail")
                    return DeployWorkflowResult(
                        success=False,
                        message="Host setup failed",
                        notification_type="negative",
                        step_status=step_status,
                    )
                on_log("  Host setup complete")

                # 1b. Verify Proxmox storage is active before tofu
                on_log("  Verifying Proxmox storage is active...")
                for _ in range(12):
                    lease.raise_if_cancelled()
                    storage_active = await _check_storage_active(root)
                    if storage_active:
                        break
                    await asyncio.sleep(10)
                if not storage_active:
                    on_log("  Proxmox storage not active after host-setup")
                    set_step(STEP_INFRA, "fail")
                    return DeployWorkflowResult(
                        success=False,
                        message="Proxmox storage not active",
                        notification_type="negative",
                        step_status=step_status,
                    )
                on_log("  Storage active")

                # 2. Provision LXCs with OpenTofu
                on_log("  Provisioning LXCs (tofu init + apply)...")
                from toolkit.core.infra.infra_env import load_tofu_env

                tofu_env = await run_blocking(load_tofu_env, root)
                proc = await asyncio.create_subprocess_exec(
                    tofu_bin,
                    "init",
                    "-input=false",
                    cwd=str(infra_dir),
                    env=tofu_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=10 * 1024 * 1024,
                )
                await asyncio.wait_for(proc.communicate(), timeout=60)
                lease.raise_if_cancelled()
                if proc.returncode != 0:
                    on_log("  tofu init failed")
                    set_step(STEP_INFRA, "fail")
                    return DeployWorkflowResult(
                        success=False,
                        message="tofu init failed",
                        notification_type="negative",
                        step_status=step_status,
                    )

                proc = await asyncio.create_subprocess_exec(
                    tofu_bin,
                    "apply",
                    "-auto-approve",
                    "-input=false",
                    cwd=str(infra_dir),
                    env=tofu_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=10 * 1024 * 1024,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                lease.raise_if_cancelled()
                if proc.returncode != 0:
                    on_log(f"  tofu apply failed: {stderr.decode(errors='replace')[:500]}")
                    set_step(STEP_INFRA, "fail")
                    return DeployWorkflowResult(
                        success=False,
                        message="Infrastructure provisioning failed",
                        notification_type="negative",
                        step_status=step_status,
                    )
                on_log("  LXCs provisioned")

                # 3. Generate inventory from tofu outputs
                on_log("  Generating Ansible inventory from tofu outputs...")
                tofu_ips = parse_tofu_machine_ips(infra_dir)
                write_inventory(root, cfg, machine_ips=tofu_ips)
                on_log("  ✓ Infrastructure provisioned successfully")
                set_step(STEP_INFRA, "ok")
                infra_freshly_provisioned = True

            except TimeoutError:
                on_log("  Infrastructure provisioning timed out")
                set_step(STEP_INFRA, "fail")
                return DeployWorkflowResult(
                    success=False,
                    message="Infrastructure provisioning timed out",
                    notification_type="negative",
                    step_status=step_status,
                )

        try:
            for secret_line in await run_blocking(reconcile_infrastructure_secrets, root):
                on_log(f"  {secret_line}")
        except RuntimeError as exc:
            on_log(f"  Infrastructure credential reconciliation failed: {exc}")
            set_step(STEP_INFRA, "fail")
            return DeployWorkflowResult(
                success=False,
                message="Infrastructure credential reconciliation failed",
                notification_type="negative",
                step_status=step_status,
            )

        from toolkit.core.compose.docker import compose_for_root

        all_ok = True
        use_ansible = cfg.proxmox.provision_machines and cfg.is_multi_node
        inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"

        for vm in target_vms:
            set_step(deploy_step_id(vm), "running")

        on_log("\nStep: Deploying to all nodes...")
        lease.raise_if_cancelled()

        if not use_ansible:
            on_log("  Checking Docker daemon...")
            dc = compose_for_root(cfg, root)
            if not dc or not dc.preflight():
                on_log("  ⚠ Docker pre-flight check failed — cannot deploy.")
                for vm in target_vms:
                    set_step(deploy_step_id(vm), "fail")
                _skip_remaining_steps(set_step)
                return DeployWorkflowResult(
                    success=False,
                    message="Docker daemon unavailable",
                    notification_type="negative",
                    step_status=step_status,
                )
            on_log("  ✓ Docker daemon reachable")
        else:
            on_log("  Remote LXC deploy — checking SSH connectivity...")
            selected_vms = tuple(target_vms)
            if infra_freshly_provisioned or _guests_need_bootstrap(cfg, root, vms=selected_vms):
                bootstrap_pb = root / "automation" / "ansible" / "playbooks" / "bootstrap-lxc.yml"
                on_log("  Bootstrapping guests (Docker/SSH) before image sync...")
                progress.set_step("Ansible (bootstrap-lxc)")
                boot_rc = await _run_ansible_playbook_file(
                    root,
                    inventory,
                    bootstrap_pb,
                    on_log,
                    progress=progress,
                    limit=ansible_target_limit(targets),
                )
                if boot_rc != 0:
                    for vm in target_vms:
                        set_step(deploy_step_id(vm), "fail")
                    _skip_remaining_steps(set_step)
                    return DeployWorkflowResult(
                        success=False,
                        message="bootstrap-lxc failed on fresh LXCs",
                        notification_type="negative",
                        step_status=step_status,
                    )

            from toolkit.core.infra.ssh_probe import probe_ssh_connectivity

            ssh_lines = probe_ssh_connectivity(cfg, root, targets=tuple(target_vms))
            for ssh_line in ssh_lines:
                on_log(f"  {ssh_line}")
            if any("FAIL" in ssh_line for ssh_line in ssh_lines):
                for vm in target_vms:
                    set_step(deploy_step_id(vm), "fail")
                _skip_remaining_steps(set_step)
                return DeployWorkflowResult(
                    success=False,
                    message="SSH preflight failed — fix connectivity before deploy",
                    notification_type="negative",
                    step_status=step_status,
                )
            on_log("  ✓ SSH reachable on all target VMs")

            img_fail = await _ensure_guest_custom_images(cfg, root, on_log, vms=tuple(target_vms))
            if img_fail:
                for vm in target_vms:
                    set_step(deploy_step_id(vm), "fail")
                img_fail.step_status = step_status
                return img_fail

        if use_ansible and inventory.exists():
            playbook, label = select_guest_deploy_playbook(root, skip_infra=skip_infra)
            if not playbook.exists():
                on_log("  ⚠ No Ansible guest playbook found")
                all_ok = False
            else:
                on_log(f"  Deploying via Ansible ({label}) to remote LXCs...")
                from toolkit.core.ansible.ansible_ssh import refresh_known_hosts_file

                for kh_line in refresh_known_hosts_file(root, cfg):
                    on_log(f"  {kh_line}")
                current_vm: str | None = None
                ansible_output_lines: list[str] = []
                progress.set_step(f"Ansible ({label})")

                def capture_ansible_output(text: str) -> None:
                    nonlocal all_ok, current_vm
                    ansible_output_lines.append(text)
                    for vm in target_vms:
                        host = cfg.machines[vm].hostname
                        if f"PLAY [{host}]" in text:
                            if current_vm and current_vm != vm:
                                set_step(deploy_step_id(current_vm), "ok")
                            current_vm = vm
                        if "fatal:" in text.lower() and current_vm:
                            all_ok = False

                from toolkit.core.ansible.ansible_runner import run_playbook_streaming

                extra_vars: dict[str, bool] = {}
                if os.environ.get("HOMELAB_FORCE_COMPOSE", "").strip() in ("1", "true", "yes"):
                    extra_vars["homelab_force_compose"] = True
                if preserve_controller:
                    extra_vars["homelab_preserve_controller"] = True
                ansible_returncode = await run_playbook_streaming(
                    root,
                    playbook,
                    inventory,
                    on_log,
                    limit=ansible_target_limit(targets),
                    extra_vars=extra_vars or None,
                    progress=progress,
                    on_output=capture_ansible_output,
                )
                lease.raise_if_cancelled()

                # Parse Ansible PLAY RECAP for unreachable/failed hosts
                import re as _re

                unreachable_hosts: list[str] = []
                failed_hosts: list[str] = []
                in_recap = False
                for line_text in ansible_output_lines:
                    if "PLAY RECAP" in line_text:
                        in_recap = True
                        continue
                    if in_recap and line_text.strip() and "=>" not in line_text:
                        m = _re.search(r"unreachable=(\d+)", line_text)
                        if m and int(m.group(1)) > 0:
                            host_part = line_text.split(":")[0].strip()
                            unreachable_hosts.append(f"{host_part} ({m.group(1)} unreachable)")
                        m = _re.search(r"failed=(\d+)", line_text)
                        if m and int(m.group(1)) > 0:
                            host_part = line_text.split(":")[0].strip()
                            failed_hosts.append(f"{host_part} ({m.group(1)} failed)")
                    elif in_recap and not line_text.strip():
                        break

                if unreachable_hosts:
                    all_ok = False
                    on_log(f"  ✗ Ansible unreachable hosts: {', '.join(unreachable_hosts)}")
                if failed_hosts:
                    all_ok = False
                    on_log(f"  ✗ Ansible failed hosts: {', '.join(failed_hosts)}")
                if not unreachable_hosts and not failed_hosts and ansible_returncode == 0:
                    on_log("  ✓ Ansible host reachability: all hosts OK (0 unreachable, 0 failed)")

                if current_vm:
                    set_step(deploy_step_id(current_vm), "ok" if ansible_returncode == 0 else "fail")
                for vm in target_vms:
                    if step_status.get(deploy_step_id(vm)) == "running":
                        set_step(deploy_step_id(vm), "ok" if ansible_returncode == 0 else "fail")
                if ansible_returncode != 0:
                    all_ok = False
                    on_log(f"  ✗ Ansible deploy failed (exit {ansible_returncode})")
                else:
                    on_log("  ✓ Ansible deploy finished on all guests")
        else:
            for vm in target_vms:
                lease.raise_if_cancelled()
                ip = cfg.node_ip(vm)
                on_log(f"  [{vm}] Deploying to {ip} (local Docker)...")
                try:
                    result = await run_blocking(deploy_local, root, vm, cfg)
                    if result.success:
                        on_log(f"  [{vm}] ✓ {len(result.services_started)} services started")
                        set_step(deploy_step_id(vm), "ok")
                    else:
                        all_ok = False
                        set_step(deploy_step_id(vm), "fail")
                        on_log(f"  [{vm}] ✗ {result.error}")
                except (OSError, ValueError, RuntimeError) as exc:
                    all_ok = False
                    set_step(deploy_step_id(vm), "fail")
                    on_log(f"  [{vm}] ✗ Error: {exc}")

        if not all_ok:
            on_log("\n⚠ Skipping hooks/verify — some nodes failed deployment. Fix errors and re-deploy.")
            _skip_remaining_steps(set_step)
            return DeployWorkflowResult(
                success=False,
                message="Deployment finished with errors",
                notification_type="warning",
                step_status=step_status,
            )

        set_step(STEP_HOOKS, "running")
        on_log("\nStep: Running post-start hooks...")
        hooks_ok = True
        hook_results: dict[str, list[str]] = {}
        hook_audits: dict[str, HookAuditSummary] = {}
        try:
            from toolkit.core.deploy.hook_audit import audit_hook_results, format_hook_audit, save_last_hooks_report

            for vm in target_vms:
                lease.raise_if_cancelled()
                vm_hooks, vm_ok = await run_blocking(run_post_start_hooks_remote, cfg, root, vm)
                credential_logs = await run_blocking(reconcile_runtime_credentials, cfg, root, vm)
                if credential_logs:
                    vm_hooks.setdefault(vm, []).extend(credential_logs)
                hook_results.update(vm_hooks)
                flat = vm_hooks.get(vm, [])
                audit = audit_hook_results({vm: flat}, vm_hint=vm)
                hook_audits[vm] = audit
                if not vm_ok or not audit.passed:
                    hooks_ok = False
            save_last_hooks_report(root, hook_audits)
            if use_ansible and inventory.exists():
                on_log("  Post-start hooks executed on each guest VM via SSH")
            for vm, audit in hook_audits.items():
                on_log(f"  {format_hook_audit(audit)}")
            if hooks_ok:
                on_log("  ✓ Hooks complete")
                if cfg.is_multi_node:
                    try:
                        from toolkit.core.identity.ldap_automation import sync_sssd_after_hooks

                        for line in await run_blocking(sync_sssd_after_hooks, root, cfg):
                            on_log(f"  {line}")
                    except (OSError, ValueError, RuntimeError) as ldap_exc:
                        on_log(f"  LDAP sync warning: {ldap_exc}")
            else:
                on_log("  ✗ Hooks reported issues — see summary above")
            set_step(STEP_HOOKS, "ok" if hooks_ok else "fail")
        except OperationCancelledError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  Hooks error: {exc}")
            hooks_ok = False
            hook_results = {}
            set_step(STEP_HOOKS, "fail")

        set_step(STEP_VAULTWARDEN, "running")
        vaultwarden_ok = True
        secrets_dict: dict[str, str] = {}
        on_log("\nStep: Syncing passwords to Vaultwarden...")
        try:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.secrets.secrets import load_secrets_plaintext
            from toolkit.services.vaultwarden.bootstrap import sync_catalog_to_vaultwarden

            secrets_dict = await run_blocking(load_secrets_plaintext, secrets_path(root))
            vw_logs = await run_blocking(sync_catalog_to_vaultwarden, root, cfg, secrets_dict)
            for vw_line in vw_logs:
                on_log(f"  {vw_line}")
            set_step(STEP_VAULTWARDEN, "ok")
            on_log("  ✓ Vaultwarden sync complete")
        except RuntimeError as exc:
            on_log(f"  ✗ Vaultwarden sync failed: {exc}")
            vaultwarden_ok = False
            set_step(STEP_VAULTWARDEN, "fail")
        except (OSError, ValueError) as exc:
            on_log(f"  ⚠ Vaultwarden sync error: {exc}")
            vaultwarden_ok = False
            set_step(STEP_VAULTWARDEN, "fail")

        # DNS must converge before service verification so checks for newly
        # added or renamed records observe the desired public state.
        set_step(STEP_DNS, "running")
        lease.raise_if_cancelled()
        from toolkit.core.ops.controller_guard import skip_message, skip_on_workstation

        if skip_dns or skip_on_workstation("dns_sync"):
            reason = "flag" if skip_dns else "workstation"
            on_log(f"\nStep: DNS sync skipped ({reason}).")
            if skip_on_workstation("dns_sync") and not skip_dns:
                on_log(f"  {skip_message('dns_sync')}")
            set_step(STEP_DNS, "skip")
        else:
            on_log("\nStep: Syncing DNS records to Cloudflare...")
            from toolkit.core.ops.dns import cloudflare_client_from_root, sync_cloudflare_dns

            try:
                await run_blocking(cloudflare_client_from_root, root)
            except ValueError:
                on_log("  ⚠ Skipped: No Cloudflare API token configured.")
                set_step(STEP_DNS, "skip")
            else:
                try:
                    await run_blocking(sync_cloudflare_dns, root, on_log=lambda m: on_log(f"  {m}"))
                    from toolkit.core.ops.dns import resolve_public_dns_ip, verify_dns_propagation

                    public_ip, _src = resolve_public_dns_ip(cfg)
                    if public_ip:
                        dns_proxied = bool(getattr(cfg.dns, "proxy_enabled", True))
                        target = "a globally routable IPv4 (Cloudflare proxy)" if dns_proxied else public_ip
                        on_log(f"  Checking DNS propagation for {cfg.domain} → {target}...")
                        propagated = await run_blocking(
                            verify_dns_propagation,
                            cfg.domain,
                            public_ip,
                            5,
                            10,
                            proxied=dns_proxied,
                        )
                        if propagated:
                            on_log("  ✓ DNS propagation OK")
                        else:
                            on_log("  ⚠ DNS not propagated yet (may take a few minutes)")
                    set_step(STEP_DNS, "ok")
                except (OSError, ValueError, RuntimeError) as exc:
                    on_log(f"  ⚠ DNS sync failed: {exc}")
                    set_step(STEP_DNS, "fail")

        set_step(STEP_HOOK_VERIFY, "running")
        hook_verify_ok = True
        on_log("\nStep: Verifying hook configuration via service APIs...")
        try:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.ops.hook_verify import HookVerifyResult, VerifyCheck, format_verify_report, verify_hooks
            from toolkit.core.secrets.secrets import load_secrets_plaintext

            secrets_dict = await run_blocking(load_secrets_plaintext, secrets_path(root))

            # Retry failed checks up to 3 times with 30s delay — services may
            # still be starting up and need time to become responsive.
            max_retries = 3
            retry_delay = 30
            hook_result = None
            accumulated_checks: dict[tuple[str, str], VerifyCheck] = {}
            retry_services: frozenset[str] | None = None
            for attempt in range(1, max_retries + 1):
                lease.raise_if_cancelled()
                hook_result = HookVerifyResult()
                hook_targets: list[str | None] = list(target_vms) if targets is not None else [None]
                for hook_target in hook_targets:
                    current = await run_blocking(
                        verify_hooks,
                        cfg,
                        secrets_dict,
                        root,
                        vm=hook_target,
                        on_progress=lambda message: on_log(f"  → {message}"),
                        only_services=retry_services,
                    )
                    for verify_check in current.checks:
                        accumulated_checks[(verify_check.service, verify_check.check)] = verify_check
                hook_result.checks = list(accumulated_checks.values())
                if hook_result.all_passed:
                    hook_report = format_verify_report(hook_result)
                    hook_summary = hook_result.summary
                    for log_line in hook_report.splitlines():
                        on_log(f"  {log_line}")
                    hook_verify_ok = True
                    on_log(f"  All checks passed on attempt {attempt}")
                    break
                if attempt < max_retries and hook_result.retryable_failures:
                    failed = hook_result.failed_checks
                    retry_services = frozenset(check.service for check in hook_result.retryable_failures)
                    on_log(
                        f"  {len(failed)} check(s) failed on attempt {attempt}/{max_retries}"
                        f" — retrying {len(retry_services)} affected service(s) in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                    lease.raise_if_cancelled()
                else:
                    hook_report = format_verify_report(hook_result)
                    hook_summary = hook_result.summary
                    for log_line in hook_report.splitlines():
                        on_log(f"  {log_line}")
                    hook_verify_ok = False
                    hooks_ok = False
                    if attempt < max_retries:
                        on_log("  ✗ Non-retryable checks still failing; stopping retries")
                    else:
                        on_log(f"  ✗ Some checks still failing after {max_retries} attempts")
                    break

            set_step(STEP_HOOK_VERIFY, "ok" if hook_verify_ok else "fail")
        except OperationCancelledError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  ⚠ Hook verification error: {exc}")
            hook_verify_ok = False
            hooks_ok = False
            set_step(STEP_HOOK_VERIFY, "fail")

        set_step(STEP_VERIFY, "running")
        lease.raise_if_cancelled()
        on_log("\nStep: Verifying deployment...")
        verify_results = None
        verify_ok = True
        try:
            verify_results = {}
            verify_targets: list[str | None] = list(target_vms) if targets is not None else [None]
            for verify_target in verify_targets:
                if use_ansible and inventory.exists() and shutil.which("ansible"):
                    current_results = await run_blocking(verify_remote, root, cfg, vm=verify_target)
                else:
                    current_results = await run_blocking(verify_all, root, cfg, vm=verify_target)
                verify_results.update(current_results)
            report = format_report(verify_results)
            for log_line in report.splitlines():
                on_log(f"  {log_line}")
            verify_ok = all(r.ok for r in verify_results.values())
            set_step(STEP_VERIFY, "ok" if verify_ok else "fail")
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  ⚠ Verify error: {exc}")
            set_step(STEP_VERIFY, "fail")
            verify_ok = False

        # Soak step: wait briefly, then re-check container health.
        # Catches containers that start healthy then crash shortly after
        # (common for JVM warmup, migration loops, OOM after first request).
        # Non-fatal — just logs warnings. The watchdog timer will alert on
        # persistent failures.
        soak_seconds = deploy_soak_seconds()
        if verify_ok and target_vms and soak_seconds > 0:
            on_log(f"\nStep: Soak — re-checking container health after {soak_seconds}s...")
            await asyncio.sleep(soak_seconds)
            lease.raise_if_cancelled()
            if use_ansible and inventory.exists():
                for soak_vm in target_vms:
                    lease.raise_if_cancelled()
                    try:
                        soak_results = await run_blocking(verify_remote, root, cfg, vm=soak_vm)
                        soak_ok = all(r.ok for r in soak_results.values())
                        if not soak_ok:
                            on_log(f"  ⚠ Soak: {soak_vm} has unhealthy containers after {soak_seconds}s")
                            for name, soak_result in soak_results.items():
                                if not soak_result.ok:
                                    detail = ", ".join(soak_result.errors[:2]) or "unhealthy"
                                    on_log(f"    • {name}: {detail[:80]}")
                        else:
                            on_log(f"  ✓ Soak: {soak_vm} healthy after {soak_seconds}s")
                    except Exception as exc:
                        on_log(f"  ⚠ Soak check skipped for {soak_vm}: {exc}")
            else:
                try:
                    soak_results = await run_blocking(verify_all, root, cfg)
                    soak_ok = all(r.ok for r in soak_results.values())
                    if soak_ok:
                        on_log(f"  ✓ Soak: local containers healthy after {soak_seconds}s")
                    else:
                        on_log(f"  ⚠ Soak: local containers unhealthy after {soak_seconds}s")
                        for name, soak_result in soak_results.items():
                            if not soak_result.ok:
                                detail = ", ".join(soak_result.errors[:2]) or "unhealthy"
                                on_log(f"    • {name}: {detail[:80]}")
                except Exception as exc:
                    on_log(f"  ⚠ Soak check skipped for local deployment: {exc}")

        set_step(STEP_QA, "running")
        lease.raise_if_cancelled()
        qa_ok = True
        on_log("\nStep: Infrastructure QA (image availability and provider policy)...")
        try:
            from toolkit.core.deploy.deploy_qa import run_infrastructure_qa

            qa_result = await run_blocking(run_infrastructure_qa, root, cfg, vms=targets, on_log=on_log)
            qa_ok = qa_result.ok
            set_step(STEP_QA, "ok" if qa_ok else "fail")
            if not qa_ok:
                verify_ok = False
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  ⚠ Extended QA error: {exc}")
            qa_ok = False
            set_step(STEP_QA, "fail")

        # ── Post-deploy cleanup: prune dangling images + stopped containers ───
        # Old images from updated services + orphaned stopped containers (profile
        # switches) accumulate GBs per deploy cycle. Prune them now — keep tagged
        # rollback images (only dangling <none>:<none> + stopped containers).
        set_step(STEP_CLEANUP, "running")
        lease.raise_if_cancelled()
        on_log("\nStep: Post-deploy cleanup (dangling images + stopped containers)...")
        try:
            cleanup_skipped = os.environ.get("HOMELAB_SKIP_DEPLOY_CLEANUP", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            if cleanup_skipped:
                on_log("  ○ Skipped by HOMELAB_SKIP_DEPLOY_CLEANUP")
                set_step(STEP_CLEANUP, "skip")
            else:
                from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

                cleaned_vms: list[str] = []
                if use_ansible:
                    for vm in target_vms:
                        lease.raise_if_cancelled()
                        vm_ip = cfg.node_ip(vm)
                        # Prune dangling images + stopped containers on each guest LXC.
                        # Keep tagged images (rollback safety); only remove <none>:<none>.
                        for label, cmd in (
                            ("stopped containers", "docker container prune -f"),
                            ("dangling images", "docker image prune -f"),
                        ):
                            rc, out, _ = await run_blocking(
                                ssh_run_on_vm,
                                cfg,
                                vm_ip,
                                cmd,
                                root=root,
                                timeout=60,
                            )
                            if rc == 0 and out.strip():
                                on_log(f"  {vm}: pruned {label}")
                        cleaned_vms.append(vm)
                # Also clean the controller's own docker (build artifacts).
                try:
                    import subprocess

                    if shutil.which("docker"):
                        subprocess.run(
                            ["docker", "image", "prune", "-f"],
                            capture_output=True,
                            timeout=60,
                            check=False,
                        )
                        on_log("  controller: pruned dangling images")
                except Exception:
                    pass
                if cleaned_vms:
                    on_log(f"  ✓ Cleanup done on {', '.join(cleaned_vms)} + controller")
                else:
                    on_log("  ✓ Nothing to clean")
                set_step(STEP_CLEANUP, "ok")
        except OperationCancelledError:
            raise
        except Exception as exc:
            on_log(f"  ⚠ Cleanup error (non-fatal): {exc}")
            set_step(STEP_CLEANUP, "skip")

        dns_failed = step_status.get(STEP_DNS) == "fail"
        hook_step_failed = step_status.get(STEP_HOOKS) == "fail" or step_status.get(STEP_HOOK_VERIFY) == "fail"
        all_success = hooks_ok and vaultwarden_ok and verify_ok and qa_ok and not dns_failed and not hook_step_failed
        if all_success and set(target_vms) == set(cfg.enabled_nodes) and (root / "toolkit" / "services").is_dir():
            try:
                from toolkit.core.manifest.catalog import load_service_catalog
                from toolkit.core.manifest.ownership import commit_ownership_ledger, current_ownership

                removed_secrets = commit_ownership_ledger(root, current_ownership(cfg, load_service_catalog(root)))
                if removed_secrets:
                    on_log(f"  ✓ Pruned {len(removed_secrets)} removed service credential(s)")
            except (OSError, RuntimeError, ValueError) as exc:
                on_log(f"  ✗ Could not commit verified service ownership: {exc}")
                all_success = False
        if all_success:
            on_log("\n✓ Full deployment complete!")
        else:
            on_log("\n✗ Deployment finished with blocking issues.")
        notif: DeployNotificationType
        if all_success:
            message = "Deployment complete!"
            notif = "positive"
        elif not hooks_ok or hook_step_failed:
            message = "Deployment failed: post-start hooks or hook verification failed"
            notif = "negative"
        elif not vaultwarden_ok:
            message = "Deployment failed: Vaultwarden sync incomplete — check logs"
            notif = "negative"
        elif not verify_ok:
            message = "Deployment failed: verification found issues"
            notif = "negative"
        elif dns_failed:
            message = "Deployment complete (DNS sync had issues — check logs)"
            notif = "warning"
        else:
            message = "Deployment finished with issues — check logs"
            notif = "warning"

        on_log("\n=== Manual steps (not fully automatable) ===")
        try:
            from toolkit.core.ops.manual_steps import format_manual_steps_cli, get_all_manual_guidance

            for log_line in format_manual_steps_cli(
                get_all_manual_guidance(cfg, hook_results, secrets=secrets_dict)
            ).splitlines():
                on_log(log_line)
        except Exception as exc:
            on_log(f"  (could not render manual steps: {exc})")

        set_step(STEP_NOTIFY, "running")
        lease.raise_if_cancelled()
        on_log("\nStep: Sending deploy notification...")
        notify_sent = False
        try:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.deploy.deploy_notify import send_deploy_notification
            from toolkit.core.secrets.secrets import load_secrets_plaintext

            secrets_dict = await run_blocking(load_secrets_plaintext, secrets_path(root))
            verify_summary = ""
            if verify_results:
                passed = sum(1 for r in verify_results.values() if r.ok)
                verify_summary = f"Verify: {passed}/{len(verify_results)} nodes OK"
            notify_sent = await run_blocking(
                send_deploy_notification,
                cfg,
                secrets_dict,
                success=all_success,
                message=message,
                notification_type=notif,
                step_status=dict(step_status),
                hook_summary=hook_summary or "",
                verify_summary=verify_summary,
            )
            if notify_sent:
                on_log("  ✓ Notification sent")
                set_step(STEP_NOTIFY, "ok")
            else:
                url = secrets_dict.get("DEPLOY_NTFY_URL") or getattr(cfg.notifications, "deploy_ntfy_url", "")
                if not url:
                    on_log("  ○ Skipped (set DEPLOY_NTFY_URL in secrets or notifications.deploy_ntfy_url)")
                    set_step(STEP_NOTIFY, "skip")
                else:
                    on_log("  ⚠ Notification failed to send")
                    set_step(STEP_NOTIFY, "fail")
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  ⚠ Notification error: {exc}")
            set_step(STEP_NOTIFY, "fail")
        lease.raise_if_cancelled()

        return DeployWorkflowResult(
            success=all_success,
            message=message,
            notification_type=notif,
            step_status=step_status,
            verify_results=verify_results,
        )
    except LeaseBusyError:
        return DeployWorkflowResult(
            success=False,
            message="Another operation is already running (.deploy.lock held)",
            notification_type="warning",
            step_status=step_status,
        )
    except OperationCancelledError as exc:
        for step, status in list(step_status.items()):
            if status == "running":
                set_step(step, "fail")
        on_log(f"\nOperation cancelled: {exc}")
        return DeployWorkflowResult(
            success=False,
            message=str(exc),
            notification_type="warning",
            step_status=step_status,
        )
    except BaseException as exc:
        for step, status in list(step_status.items()):
            if status == "running":
                set_step(step, "fail")
        on_log(f"\n✗ Deployment failed: {exc}")
        on_log("  └ Deployment error — see deploy log for full details")
        if not isinstance(exc, Exception):
            raise
        return DeployWorkflowResult(
            success=False,
            message=f"Deployment failed: {exc}",
            notification_type="negative",
            step_status=step_status,
        )
    finally:
        if owns_lease and lease is not None:
            try:
                lease.release()
            except OSError as exc:
                log.warning("Failed to release deploy lock: %s", exc)


async def run_recover_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None = None,
    vm: str | None = None,
    operation_lease: OperationLease | None = None,
) -> DeployWorkflowResult:
    """Compatibility wrapper for the extracted recovery workflow."""
    from toolkit.core.deploy import deploy_recovery

    return await deploy_recovery.run_recover_workflow(
        root,
        cfg,
        on_log=on_log,
        on_step=on_step,
        on_progress=on_progress,
        vm=vm,
        operation_lease=operation_lease,
        run_preflight_fn=run_preflight,
        preflight_passed_fn=preflight_passed,
        ensure_guest_custom_images_fn=_ensure_guest_custom_images,
        run_post_start_hooks_remote_fn=run_post_start_hooks_remote,
        reconcile_runtime_credentials_fn=reconcile_runtime_credentials,
        verify_remote_fn=verify_remote,
        verify_all_fn=verify_all,
    )


async def run_clean_wipe_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None = None,
    wipe_zfs: bool = False,
) -> DeployWorkflowResult:
    """Compatibility wrapper for the extracted clean-wipe workflow."""
    from toolkit.core.deploy import deploy_wipe

    return await deploy_wipe.run_clean_wipe_workflow(
        root,
        cfg,
        on_log=on_log,
        on_step=on_step,
        on_progress=on_progress,
        wipe_zfs=wipe_zfs,
        run_preflight_fn=run_preflight,
        preflight_passed_fn=preflight_passed,
        sync_from_repo_root_fn=sync_from_repo_root,
    )


def _cleanup_lease(lease: OperationLease | None, result: DeployWorkflowResult) -> DeployWorkflowResult:
    """Release the operation lease and return the workflow result."""
    if lease is not None:
        try:
            lease.release()
        except OSError as exc:
            log.warning("Failed to release operation lease: %s", exc)
    return result
