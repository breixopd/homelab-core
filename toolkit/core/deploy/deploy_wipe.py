"""Clean-wipe deployment workflow implementation."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from toolkit.core.ansible.ansible_inventory import generated_extra_vars
from toolkit.core.ansible.ansible_ssh import resolve_tool
from toolkit.core.async_utils import run_blocking
from toolkit.core.config.config import Config
from toolkit.core.deploy.deploy_log import DeployProgressReporter, ProgressCallback
from toolkit.core.deploy.deploy_workflow import (
    _ANSIBLE_STREAM_LIMIT,
    STEP_GENERATE,
    STEP_INFRA,
    STEP_PREFLIGHT,
    STEP_VERIFY,
    DeployWorkflowResult,
    _cleanup_lease,
    _init_step_status,
    workflow_step_labels,
)
from toolkit.core.deploy.destructive_guard import RecoveryCheckpointRequiredError
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationCancelledError, OperationLease
from toolkit.core.infra.iac_sync import sync_from_repo_root
from toolkit.core.infra.infra_destroy import clean_tofu_state, destroy_infrastructure
from toolkit.core.ops.preflight import preflight_passed, run_preflight
from toolkit.core.ops.verify import format_report, verify_remote
from toolkit.core.secrets.secrets import load_secrets_plaintext

log = logging.getLogger(__name__)


async def run_clean_wipe_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None = None,
    wipe_zfs: bool = False,
    run_preflight_fn=run_preflight,
    preflight_passed_fn=preflight_passed,
    sync_from_repo_root_fn=sync_from_repo_root,
) -> DeployWorkflowResult:
    """Run clean-wipe while owning one exception-safe operation lease."""
    root = root.resolve()
    step_status = _init_step_status(cfg)
    try:
        lease = OperationLease.acquire(root, "clean-wipe")
    except LeaseBusyError:
        return DeployWorkflowResult(
            success=False,
            message="Another operation is already running (.deploy.lock held)",
            notification_type="negative",
            step_status=step_status,
        )
    except OSError as exc:
        return DeployWorkflowResult(
            success=False,
            message=f"Could not acquire deploy lock: {exc}",
            notification_type="negative",
            step_status=step_status,
        )

    try:
        return await _run_clean_wipe_workflow_owned(
            root,
            cfg,
            on_log=on_log,
            on_step=on_step,
            on_progress=on_progress,
            wipe_zfs=wipe_zfs,
            lease=lease,
            step_status=step_status,
            run_preflight_fn=run_preflight_fn,
            preflight_passed_fn=preflight_passed_fn,
            sync_from_repo_root_fn=sync_from_repo_root_fn,
        )
    except OperationCancelledError as exc:
        for step, status in list(step_status.items()):
            if status == "running":
                step_status[step] = "fail"
        on_log(f"Operation cancelled: {exc}")
        return DeployWorkflowResult(
            success=False,
            message=str(exc),
            notification_type="warning",
            step_status=step_status,
        )
    except RecoveryCheckpointRequiredError as exc:
        on_log(f"Clean wipe refused: {exc}")
        return DeployWorkflowResult(
            success=False,
            message=f"Clean wipe refused: {exc}",
            notification_type="negative",
            step_status=step_status,
        )
    finally:
        try:
            lease.release()
        except OSError as exc:
            log.warning("Failed to release clean-wipe lock: %s", exc)


async def _run_clean_wipe_workflow_owned(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None,
    wipe_zfs: bool,
    lease: OperationLease,
    step_status: dict[str, str],
    run_preflight_fn=run_preflight,
    preflight_passed_fn=preflight_passed,
    sync_from_repo_root_fn=sync_from_repo_root,
) -> DeployWorkflowResult:
    """Full clean-wipe redeploy: destroy everything and rebuild from scratch.

    Equivalent to the clean-wipe.sh shell script, now in Python for
    better error handling and integration with the deploy workflow system.
    """
    root = root.resolve()
    hostname_to_node = {machine.hostname: node for node, machine in cfg.machines.items()}
    progress = DeployProgressReporter(
        on_log=on_log,
        on_progress=on_progress,
        step="Clean wipe",
        hostname_to_node=hostname_to_node,
    )
    overall_start = time.monotonic()

    def _elapsed(since: float) -> str:
        secs = int(time.monotonic() - since)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m{secs % 60:02d}s"

    def set_step(step: str, status: str) -> None:
        step_status[step] = status
        try:
            on_step(step, status)
        except Exception as exc:
            log.warning("clean_wipe set_step callback failed: %s", exc)
        if status == "running":
            progress.set_step(workflow_step_labels(cfg).get(step, step))

    ansible_dir = root / "automation" / "ansible"
    infra_dir = root / "infrastructure"
    inventory = ansible_dir / "inventory" / "hosts.yml"

    # ------------------------------------------------------------------
    # 0. Enhanced pre-deploy validation
    # ------------------------------------------------------------------
    on_log("=== Clean Wipe: Pre-deploy Validation ===")

    # API token check
    from toolkit.core.config.storage import secrets_path as _sp

    try:
        sec = load_secrets_plaintext(_sp(root))
        token = sec.get("PROXMOX_API_TOKEN_ID") or os.environ.get("PROXMOX_API_TOKEN_ID")
        if not token:
            on_log("  ⚠ PROXMOX_API_TOKEN_ID not found in secrets or env — IaC may fail")
    except Exception:
        on_log("  ⚠ Could not check Proxmox API token — ensure it is set")

    # Proxmox reachability check
    if cfg.proxmox.api_url:
        from urllib.parse import urlparse

        parsed = urlparse(cfg.proxmox.api_url)
        netloc = parsed.hostname or ""
        if netloc:
            on_log(f"  Proxmox API: {cfg.proxmox.api_url} (TLS verification required)")

    # ZFS disks check (warn if wipe-zfs requested but zfs not enabled)
    if wipe_zfs:
        try:
            zfs_cfg = getattr(cfg.storage, "zfs_pool", "data") if hasattr(cfg, "storage") else "data"
            on_log(f"  ZFS pool: {zfs_cfg} (will be destroyed and recreated)")
        except Exception as exc:
            log.warning("Failed to read ZFS config for clean wipe: %s", exc)

    # ------------------------------------------------------------------
    # 1. Preflight
    # ------------------------------------------------------------------
    set_step(STEP_PREFLIGHT, "running")
    items = run_preflight_fn(root, cfg)
    for item in items:
        on_log(f"  {'✓' if item.ok else '✗'} {item.label}")
    preflight_ok = preflight_passed_fn(items)
    set_step(STEP_PREFLIGHT, "ok" if preflight_ok else "fail")
    if not preflight_ok:
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message="Clean wipe aborted: preflight checks failed",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    from toolkit.core.deploy.destructive_guard import require_verified_checkpoint

    require_verified_checkpoint(root, cfg.enabled_nodes, timedelta(days=7))
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 2. Sync config
    # ------------------------------------------------------------------
    set_step(STEP_GENERATE, "running")
    try:
        await run_blocking(sync_from_repo_root_fn, root)
        set_step(STEP_GENERATE, "ok")
    except Exception as exc:
        on_log(f"Config sync failed: {exc}")
        set_step(STEP_GENERATE, "fail")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message=f"Config sync failed: {exc}",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 3. Destroy existing LXCs and prove absence through Proxmox inventory
    # ------------------------------------------------------------------
    set_step(STEP_INFRA, "running")
    on_log("Destroying existing LXCs (OpenTofu + Proxmox inventory verification)...")

    destroy_code = await run_blocking(
        destroy_infrastructure,
        root,
        on_log=on_log,
        auto_approve=True,
    )
    if destroy_code != 0:
        set_step(STEP_INFRA, "fail")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message="Infrastructure destroy failed; preserving OpenTofu state",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 4. Wipe ZFS only after managed guests are independently proven absent
    # ------------------------------------------------------------------
    if wipe_zfs:
        on_log("Destroying ZFS pool for clean start...")
        zfs_step = "zfs_wipe"
        step_status[zfs_step] = "running"
        on_step(zfs_step, "running")
        try:
            ansible_playbook = resolve_tool("ansible-playbook", root) or "ansible-playbook"
            cmd = [
                ansible_playbook,
                "-i",
                str(inventory),
                *generated_extra_vars(root),
                "-e",
                "zfs_wipe_enabled=true",
                str(ansible_dir / "host-setup.yml"),
                "--tags",
                "zfs-wipe",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_ANSIBLE_STREAM_LIMIT,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace")[:500])
            on_log("ZFS pool destroyed successfully")
            step_status[zfs_step] = "ok"
            on_step(zfs_step, "ok")
        except OperationCancelledError:
            raise
        except Exception as exc:
            on_log(f"ZFS wipe failed: {exc}")
            step_status[zfs_step] = "fail"
            on_step(zfs_step, "fail")
            return _cleanup_lease(
                lease,
                DeployWorkflowResult(
                    success=False,
                    message="ZFS wipe failed; preserving OpenTofu state",
                    notification_type="negative",
                    step_status=step_status,
                ),
            )
        lease.raise_if_cancelled()

    # Clean tofu state for fresh start (consolidated in infra_destroy.clean_tofu_state)

    removed = clean_tofu_state(root, on_log=on_log)
    lease.raise_if_cancelled()
    if removed:
        on_log(f"Tofu state cleaned ({removed} file/dir removed)")
    set_step(STEP_INFRA, "ok")

    # ------------------------------------------------------------------
    # 5. Clean generated files
    # ------------------------------------------------------------------
    on_log("Cleaning generated files...")
    generated_dir = root / "generated"
    if generated_dir.exists():
        for entry in generated_dir.iterdir():
            if entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
    generated_dir.mkdir(parents=True, exist_ok=True)
    on_log("Generated files cleaned")

    # Re-sync after cleaning
    on_log("Re-syncing config...")
    try:
        await run_blocking(sync_from_repo_root_fn, root)
    except Exception as exc:
        on_log(f"Config re-sync failed: {exc}")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message=f"Config re-sync failed: {exc}",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 6. Host setup (ZFS + network)
    # ------------------------------------------------------------------
    host_step = "host_setup"
    step_status[host_step] = "running"
    on_step(host_step, "running")
    on_log("Running host-setup (network, ZFS, kernel modules, templates)...")
    try:
        ansible_playbook = resolve_tool("ansible-playbook", root) or "ansible-playbook"
        cmd = [
            ansible_playbook,
            "-i",
            str(inventory),
            *generated_extra_vars(root),
            str(ansible_dir / "host-setup.yml"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_ANSIBLE_STREAM_LIMIT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode == 0:
            on_log("Host setup complete")
            step_status[host_step] = "ok"
            on_step(host_step, "ok")
        else:
            on_log(f"Host setup failed: {stderr.decode(errors='replace')[:500]}")
            step_status[host_step] = "fail"
            on_step(host_step, "fail")
            return _cleanup_lease(
                lease,
                DeployWorkflowResult(
                    success=False,
                    message="Host setup failed",
                    notification_type="negative",
                    step_status=step_status,
                ),
            )
    except Exception as exc:
        on_log(f"Host setup error: {exc}")
        step_status[host_step] = "fail"
        on_step(host_step, "fail")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message=f"Host setup error: {exc}",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 7. Provision LXCs (OpenTofu)
    # ------------------------------------------------------------------
    prov_step = "provision"
    step_status[prov_step] = "running"
    on_step(prov_step, "running")
    on_log("Provisioning LXCs (tofu apply)...")
    try:
        from toolkit.core.infra.infra_env import load_tofu_env

        tofu_env = await run_blocking(load_tofu_env, root)
        # Init tofu
        proc = await asyncio.create_subprocess_exec(
            resolve_tool("tofu", root) or "tofu",
            "init",
            "-input=false",
            cwd=str(infra_dir),
            env=tofu_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_ANSIBLE_STREAM_LIMIT,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            on_log("tofu init failed")
            step_status[prov_step] = "fail"
            on_step(prov_step, "fail")
            return _cleanup_lease(
                lease,
                DeployWorkflowResult(
                    success=False,
                    message="tofu init failed",
                    notification_type="negative",
                    step_status=step_status,
                ),
            )

        # Re-sync to ensure template ID is correct
        await run_blocking(sync_from_repo_root_fn, root)
        lease.raise_if_cancelled()

        # Apply
        proc = await asyncio.create_subprocess_exec(
            resolve_tool("tofu", root) or "tofu",
            "apply",
            "-auto-approve",
            "-input=false",
            cwd=str(infra_dir),
            env=tofu_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_ANSIBLE_STREAM_LIMIT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode == 0:
            on_log("LXCs provisioned")
            step_status[prov_step] = "ok"
            on_step(prov_step, "ok")
        else:
            on_log(f"tofu apply failed: {stderr.decode(errors='replace')[:500]}")
            step_status[prov_step] = "fail"
            on_step(prov_step, "fail")
            return _cleanup_lease(
                lease,
                DeployWorkflowResult(
                    success=False,
                    message="tofu apply failed",
                    notification_type="negative",
                    step_status=step_status,
                ),
            )
    except OperationCancelledError:
        raise
    except Exception as exc:
        on_log(f"Provisioning error: {exc}")
        step_status[prov_step] = "fail"
        on_step(prov_step, "fail")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message=f"Provisioning error: {exc}",
                notification_type="negative",
                step_status=step_status,
            ),
        )
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 8. Guest setup (Ansible bootstrap + deploy)
    # ------------------------------------------------------------------
    guest_step = "guest_setup"
    step_status[guest_step] = "running"
    on_step(guest_step, "running")
    on_log("Running guest setup (bootstrap, Docker, compose, verify)...")
    try:
        from toolkit.core.ansible.ansible_runner import run_playbook_streaming

        guest_returncode = await asyncio.wait_for(
            run_playbook_streaming(
                root,
                ansible_dir / "guest-setup.yml",
                inventory,
                on_log,
            ),
            timeout=1800,
        )
        if guest_returncode == 0:
            on_log("Guest setup complete")
            step_status[guest_step] = "ok"
            on_step(guest_step, "ok")
        else:
            on_log(f"Guest setup failed (exit {guest_returncode})")
            step_status[guest_step] = "fail"
            on_step(guest_step, "fail")
    except Exception as exc:
        on_log(f"Guest setup error: {exc}")
        step_status[guest_step] = "fail"
        on_step(guest_step, "fail")
    lease.raise_if_cancelled()

    # ------------------------------------------------------------------
    # 9. Verify
    # ------------------------------------------------------------------
    set_step(STEP_VERIFY, "running")
    on_log("Running post-deploy verification...")
    try:
        verify_results = await run_blocking(verify_remote, root, cfg)
        on_log(format_report(verify_results))
        verify_ok = all(r.ok for r in verify_results.values())
    except Exception as exc:
        on_log(f"Verification error: {exc}")
        verify_ok = False
        verify_results = {}
    lease.raise_if_cancelled()
    set_step(STEP_VERIFY, "ok" if verify_ok else "fail")

    guest_ok = step_status.get(guest_step) == "ok"
    ok = guest_ok and verify_ok

    on_log(f"\n=== Clean wipe completed in {_elapsed(overall_start)} ===")

    return _cleanup_lease(
        lease,
        DeployWorkflowResult(
            success=ok,
            message=f"Clean wipe complete ({_elapsed(overall_start)})" if ok else "Clean wipe finished with issues",
            notification_type="positive" if ok else "negative",
            step_status=step_status,
            verify_results=verify_results,
        ),
    )
