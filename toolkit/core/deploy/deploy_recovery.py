"""Recovery deployment workflow implementation."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.async_utils import run_blocking
from toolkit.core.deploy.deploy_log import DeployProgressReporter, ProgressCallback
from toolkit.core.deploy.deploy_workflow import (
    STEP_HOOK_VERIFY,
    STEP_HOOKS,
    STEP_PREFLIGHT,
    STEP_VERIFY,
    DeployWorkflowResult,
    _cleanup_lease,
    _ensure_guest_custom_images,
    _init_recover_step_status,
    deploy_step_id,
    reconcile_runtime_credentials,
    run_post_start_hooks_remote,
    workflow_progress_percent,
    workflow_step_labels,
)
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationCancelledError, OperationLease
from toolkit.core.ops.preflight import PreflightProfile, preflight_passed, run_preflight
from toolkit.core.ops.verify import format_report, verify_all, verify_remote

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


async def run_recover_workflow(
    root: Path,
    cfg: Config,
    *,
    on_log: Callable[[str], None],
    on_step: Callable[[str, str], None],
    on_progress: ProgressCallback | None = None,
    vm: str | None = None,
    operation_lease: OperationLease | None = None,
    run_preflight_fn: Callable[..., list] = run_preflight,
    preflight_passed_fn: Callable[[list], bool] = preflight_passed,
    ensure_guest_custom_images_fn: Callable[..., Awaitable[DeployWorkflowResult | None]] = _ensure_guest_custom_images,
    run_post_start_hooks_remote_fn: Callable[..., tuple[dict[str, list[str]], bool]] = run_post_start_hooks_remote,
    reconcile_runtime_credentials_fn: Callable[..., list[str]] = reconcile_runtime_credentials,
    verify_remote_fn: Callable[..., dict] = verify_remote,
    verify_all_fn: Callable[..., dict] = verify_all,
) -> DeployWorkflowResult:
    """Recover deploy: ansible deploy-recover.yml then verify."""
    root = root.resolve()
    preflight_profile: PreflightProfile = (
        "controller" if os.environ.get("HOMELAB_NODE", "").strip() == cfg.control_node else "operator"
    )
    step_status = _init_recover_step_status(cfg, vm=vm)
    hostname_to_node = {machine.hostname: node for node, machine in cfg.machines.items()}
    progress = DeployProgressReporter(
        on_log=on_log,
        on_progress=on_progress,
        step="Recover deploy",
        hostname_to_node=hostname_to_node,
    )

    def set_step(step: str, status: str) -> None:
        step_status[step] = status
        try:
            on_step(step, status)
        except Exception as exc:
            log.warning("recover set_step callback failed: %s", exc)
        if status == "running":
            progress.set_step(workflow_step_labels(cfg).get(step, step))
        if on_progress is not None:
            payload = progress.snapshot.as_dict()
            payload["percent"] = str(workflow_progress_percent(step_status, cfg))
            on_progress(payload)

    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    if not playbook.exists() or not inventory.exists():
        return DeployWorkflowResult(
            success=False,
            message="deploy-recover.yml or inventory missing",
            notification_type="negative",
            step_status=step_status,
        )

    lease = operation_lease
    owns_lease = operation_lease is None
    if lease is None:
        try:
            lease = OperationLease.acquire(root, "recover")
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
        lease.assert_owns_root(root)
        set_step(STEP_PREFLIGHT, "running")
        items = run_preflight_fn(
            root,
            cfg,
            bootstrap=False,
            require_provisioning_tools=False,
            profile=preflight_profile,
        )
        for item in items:
            on_log(f"  {'✓' if item.ok else '✗'} {item.label}")
        preflight_ok = preflight_passed_fn(items)
        set_step(STEP_PREFLIGHT, "ok" if preflight_ok else "fail")
        if not preflight_ok:
            return _cleanup_lease(
                lease,
                DeployWorkflowResult(
                    success=False,
                    message="Recovery aborted: preflight checks failed",
                    notification_type="negative",
                    step_status=step_status,
                ),
            )
        lease.raise_if_cancelled()
    except OperationCancelledError as exc:
        on_log(f"Operation cancelled: {exc}")
        return _cleanup_lease(
            lease,
            DeployWorkflowResult(
                success=False,
                message=str(exc),
                notification_type="warning",
                step_status=step_status,
            ),
        )
    except BaseException:
        lease.release()
        raise

    targets = [vm] if vm else cfg.enabled_nodes
    for name in targets:
        set_step(deploy_step_id(name), "running")

    # Wrap the whole post-lock body in try/finally so any exception mid-recover
    # (playbook crash, verify_remote transport error, etc.) releases the lock
    # instead of leaving a stale `.deploy.lock` that blocks every subsequent
    # `deploy all` / `deploy recover`. The historical `pid=218719`-dead lock
    # in the live cluster was exactly this leak.
    try:
        on_log("Ensuring exact-revision custom images are available on recovery targets...")
        image_failure = await ensure_guest_custom_images_fn(cfg, root, on_log, vms=tuple(targets))
        if image_failure is not None:
            for name in targets:
                set_step(deploy_step_id(name), "fail")
            for step in (STEP_HOOKS, STEP_HOOK_VERIFY, STEP_VERIFY):
                if step_status.get(step) == "pending":
                    set_step(step, "skip")
            image_failure.step_status = step_status
            return image_failure

        on_log("Running deploy recovery playbook...")
        from toolkit.core.ansible.ansible_runner import run_playbook_streaming

        labels = workflow_step_labels(cfg)
        seen_targets: set[str] = set()

        def capture_recovery_output(text: str) -> None:
            for name in targets:
                if f"[{cfg.machines[name].hostname}]" in text:
                    seen_targets.add(name)
                    progress.set_step(labels[deploy_step_id(name)])

        ansible_returncode = await run_playbook_streaming(
            root,
            playbook,
            inventory,
            on_log,
            limit=vm,
            progress=progress,
            on_output=capture_recovery_output,
        )
        lease.raise_if_cancelled()
        for name in targets:
            if ansible_returncode == 0:
                status = "ok"
            else:
                status = "fail" if name in seen_targets else "skip"
            set_step(deploy_step_id(name), status)

        deploy_ok = ansible_returncode == 0
        for name in targets:
            if step_status.get(deploy_step_id(name)) != "ok":
                deploy_ok = False

        if not deploy_ok:
            for step in (STEP_HOOKS, STEP_HOOK_VERIFY, STEP_VERIFY):
                if step_status.get(step) == "pending":
                    set_step(step, "skip")
            return DeployWorkflowResult(
                success=False,
                message=f"Recovery playbook failed (exit {ansible_returncode})",
                notification_type="negative",
                step_status=step_status,
            )

        set_step(STEP_HOOKS, "running")
        on_log("\nStep: Running post-start hooks on recovered VM(s)...")
        hooks_ok = True
        hook_results: dict[str, list[str]] = {}
        for name in targets:
            lease.raise_if_cancelled()
            vm_hooks, vm_ok = await run_blocking(run_post_start_hooks_remote_fn, cfg, root, name)
            credential_logs = await run_blocking(reconcile_runtime_credentials_fn, cfg, root, name)
            if credential_logs:
                vm_hooks.setdefault(name, []).extend(credential_logs)
            hook_results.update(vm_hooks)
            for cat_name, cat_logs in vm_hooks.items():
                for log_line in cat_logs:
                    on_log(f"  [{cat_name}] {log_line}")
                    if log_line.startswith("Hook error:"):
                        hooks_ok = False
            if not vm_ok:
                hooks_ok = False
        set_step(STEP_HOOKS, "ok" if hooks_ok else "fail")

        set_step(STEP_HOOK_VERIFY, "running")
        hook_verify_ok = True
        try:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.ops.hook_verify import VerifyCheck, format_verify_report, verify_hooks
            from toolkit.core.secrets.secrets import load_secrets_plaintext

            secrets_dict = await run_blocking(load_secrets_plaintext, secrets_path(root))

            # Mirror the deploy path's retry (3×30s): recover re-runs hooks, and
            # the same first-boot races (AdGuard reload, Postgres init) can still
            # fire. Without this retry, a recover attempt on the AdGuard race
            # re-fails identically — the original bug.
            max_retries = 3
            retry_delay = 30
            hook_result = None
            accumulated_checks: dict[tuple[str, str], VerifyCheck] = {}
            retry_services: frozenset[str] | None = None
            for attempt in range(1, max_retries + 1):
                lease.raise_if_cancelled()
                hook_result = await run_blocking(
                    verify_hooks,
                    cfg,
                    secrets_dict,
                    root,
                    vm=vm,
                    on_progress=lambda message: on_log(f"  → {message}"),
                    only_services=retry_services,
                )
                for verify_check in hook_result.checks:
                    accumulated_checks[(verify_check.service, verify_check.check)] = verify_check
                hook_result.checks = list(accumulated_checks.values())
                if hook_result.all_passed:
                    hook_verify_ok = True
                    on_log(f"  All hook-verify checks passed on attempt {attempt}")
                    break
                if attempt < max_retries and hook_result.retryable_failures:
                    failed = hook_result.failed_checks
                    retry_services = frozenset(check.service for check in hook_result.retryable_failures)
                    on_log(
                        f"  {len(failed)} hook-verify check(s) failed on attempt {attempt}/{max_retries}"
                        f" — retrying {len(retry_services)} affected service(s) in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                    lease.raise_if_cancelled()
                else:
                    hook_verify_ok = False
                    if attempt < max_retries:
                        on_log("  ✗ Non-retryable hook-verify checks still failing; stopping retries")
                    break
            if hook_result is not None:
                for log_line in format_verify_report(hook_result).splitlines():
                    on_log(f"  {log_line}")
            if not hook_verify_ok:
                hooks_ok = False
            set_step(STEP_HOOK_VERIFY, "ok" if hook_verify_ok else "fail")
        except OperationCancelledError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            on_log(f"  Hook verification error: {exc}")
            hooks_ok = False
            set_step(STEP_HOOK_VERIFY, "fail")

        set_step(STEP_VERIFY, "running")
        lease.raise_if_cancelled()
        verify_results = {}
        for attempt in range(1, 4):
            if vm:
                verify_results = await run_blocking(verify_remote_fn, root, cfg, vm=vm)
            elif cfg.is_multi_node:
                verify_results = await run_blocking(verify_remote_fn, root, cfg)
            else:
                verify_results = await run_blocking(verify_all_fn, root, cfg)
            if all(result.ok for result in verify_results.values()):
                break
            if attempt < 3:
                failed_nodes = [name for name, result in verify_results.items() if not result.ok]
                on_log(
                    f"  container verification failed on attempt {attempt}/3 "
                    f"({', '.join(failed_nodes)}) — retrying in 15s..."
                )
                await asyncio.sleep(15)
                lease.raise_if_cancelled()
        on_log(format_report(verify_results))
        verify_ok = all(r.ok for r in verify_results.values())
        set_step(STEP_VERIFY, "ok" if verify_ok else "fail")
        lease.raise_if_cancelled()

        ok = deploy_ok and hooks_ok and hook_verify_ok and verify_ok
        return DeployWorkflowResult(
            success=ok,
            message="Recovery complete" if ok else "Recovery finished with issues",
            notification_type="positive" if ok else "negative",
            step_status=step_status,
            verify_results=verify_results,
        )
    except OperationCancelledError as exc:
        for step, status in list(step_status.items()):
            if status == "running":
                set_step(step, "fail")
        on_log(f"Operation cancelled: {exc}")
        return DeployWorkflowResult(
            success=False,
            message=str(exc),
            notification_type="warning",
            step_status=step_status,
        )
    finally:
        if owns_lease and lease is not None:
            try:
                lease.release()
            except OSError as exc:
                log.warning("Failed to release recover lock at end: %s", exc)
