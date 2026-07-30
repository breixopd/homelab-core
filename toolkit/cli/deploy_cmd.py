from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from toolkit.core.deploy.deploy_lock import DeployLockStatus


def _blocking_external_deploy(root: Path, operation_lease) -> DeployLockStatus | None:
    """Return a blocking lease unless the internal caller proves exact ownership."""
    from toolkit.core.deploy.deploy_lock import read_deploy_lock
    from toolkit.core.deploy.operation_lease import OperationLease

    lock = read_deploy_lock(root)
    if not lock.blocking:
        return None
    if operation_lease is None:
        return lock
    inspection = OperationLease.inspect(root)
    if (
        inspection.active
        and inspection.snapshot is not None
        and inspection.snapshot.lease_id == operation_lease.snapshot.lease_id
    ):
        operation_lease.raise_if_cancelled()
        return None
    return lock


@click.group()
def deploy():
    """Deploy services to nodes and hosts."""
    pass


def _step_echo(step: str, status: str, labels: dict[str, str]) -> None:
    label = labels.get(step, step)
    click.echo(f"  [{status:8}] {label}")


def _print_deploy_recap(result, steps: dict, labels: dict, logs: list, cfg=None) -> None:
    """Print a clean formatted recap at the end of a deploy — step table +
    status banner + next steps. Replaces the old one-line 'result.message'."""
    # Status banner
    if result.success:
        click.secho(f"\n{'=' * 60}", fg="green", bold=True)
        click.secho(f"  ✓ {result.message}", fg="green", bold=True)
        click.secho(f"{'=' * 60}", fg="green", bold=True)
    else:
        click.secho(f"\n{'=' * 60}", fg="red", bold=True)
        click.secho(f"  ✗ {result.message}", fg="red", bold=True)
        click.secho(f"{'=' * 60}", fg="red", bold=True)

    # Step table
    click.echo(f"\n{'Step':<40} {'Status':<10}")
    click.echo(f"{'─' * 50}")
    for step, status in steps.items():
        label = labels.get(step, step)
        glyph = {"ok": "✓", "skip": "○", "fail": "✗", "running": "…"}.get(status, "·")
        color = {"ok": "green", "skip": "cyan", "fail": "red"}.get(status, "white")
        click.secho(f"  {glyph} {label:<37} {status:<10}", fg=color)

    # Keep the recap diagnostic: totals plus warnings and failures. Successful
    # individual checks are already represented by the step table and totals.
    verify_summaries = list(dict.fromkeys(line for line in logs if line.strip().startswith("Summary:")))
    fail_hooks = list(dict.fromkeys(line for line in logs if line.strip().startswith("✗") or "Plugin error" in line))
    failed_diagnostics = {line.strip() for line in fail_hooks}
    warn_hooks = list(
        dict.fromkeys(
            line
            for line in logs
            if ("⚠" in line or "WARNING" in line.upper()) and line.strip() not in failed_diagnostics
        )
    )

    if verify_summaries or warn_hooks or fail_hooks:
        click.echo(f"\n{'Service checks':<40}")
        click.echo(f"{'─' * 50}")
        for line in verify_summaries[-1:]:
            summary = line.strip()
            counts = summary.removeprefix("Summary:").strip().split(maxsplit=1)[0]
            passed, separator, total = counts.partition("/")
            color = "green" if separator and passed.isdigit() and passed == total else "red"
            click.secho(f"  {summary}", fg=color)
        for line in warn_hooks[:5]:
            click.secho(f"  {line.strip()}", fg="yellow")
        for line in fail_hooks[:10]:
            click.secho(f"  {line.strip()}", fg="red")

    # Next steps guidance + manual steps
    click.echo(f"\n{'─' * 50}")
    if result.success:
        # Show any manual steps that still need human attention
        if cfg is not None:
            try:
                from toolkit.core.ops.manual_steps import format_manual_steps_cli, get_all_manual_guidance

                steps_list = get_all_manual_guidance(cfg)
                if steps_list:
                    manual_text = format_manual_steps_cli(steps_list)
                    click.echo(manual_text)
            except Exception:
                pass  # best-effort — don't fail the recap on manual-steps errors
        click.echo("  Next: homelab-toolkit status  (check cluster health)")
        click.echo("       homelab-toolkit watchdog check  (verify all services)")
    else:
        failed = [s for s, v in steps.items() if v == "fail"]
        if failed:
            click.echo(f"  Failed steps: {', '.join(failed)}")
        click.echo("  Try: homelab-toolkit deploy recover  (re-run failed steps)")
        click.echo("       homelab-toolkit watchdog heal   (auto-fix detected issues)")
    click.echo(f"  Log:  {len(logs)} lines captured\n")


@deploy.command("destroy-infra")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def destroy_infra(ctx, yes):
    """Destroy all managed guests through a checkpoint-bound controller approval."""
    job = _destroy_infra_via_controller(ctx, yes=yes)
    if job.state.value != "SUCCEEDED":
        message = job.error.message if job.error else "Infrastructure destruction did not complete"
        raise click.ClickException(message)
    click.echo("Infrastructure destroyed and independently verified.")


def _destroy_infra_via_controller(ctx: click.Context, *, yes: bool):
    import time as _time
    import uuid

    from toolkit.cli import load_controller_client
    from toolkit.cli.controller_jobs import wait_for_controller_job
    from toolkit.controller.client import ControllerClientError
    from toolkit.controller.contracts import DestroyInfraOperation, DestroyPlanRequest, JobRequest
    from toolkit.core.state.audit_log import AuditAction, audit

    root = Path(ctx.obj["root"])
    t0 = _time.time()
    try:
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path

        client = load_controller_client(ctx)
        scopes = load_config(config_path(root)).enabled_nodes
        plan = client.create_destruction_plan(DestroyPlanRequest(action="destroy_all", scopes=scopes))
        click.echo("Destruction plan")
        click.echo(f"  Plan:       {plan.plan_id}")
        click.echo(f"  Checkpoint: {plan.spec.checkpoint_id} ({plan.spec.checkpoint_verified_at.isoformat()})")
        click.echo(f"  Plan hash:  {plan.plan_hash}")
        if not yes:
            confirmation = click.prompt(
                'Type "DESTROY ALL MANAGED INFRASTRUCTURE" to continue',
                default="",
                show_default=False,
            )
            if confirmation != "DESTROY ALL MANAGED INFRASTRUCTURE":
                click.echo("Aborted.")
                raise click.Abort()
        approval = client.approve_plan(
            plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation="DESTROY ALL MANAGED INFRASTRUCTURE",
        )
        request = JobRequest(
            idempotency_key=f"destroy-{uuid.uuid4().hex}",
            operation=DestroyInfraOperation(
                action=plan.spec.action,
                scopes=plan.spec.scopes,
                config_revision=plan.spec.config_revision,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                approval_token=approval.token,
            ),
        )
        queued = client.submit(request)
        click.echo(f"Queued controller job {queued.job_id}")

        def show_event(event) -> None:
            stage = event.payload.get("stage")
            suffix = f" ({stage})" if isinstance(stage, str) and stage else ""
            click.echo(f"  [{event.level:<7}] {event.message}{suffix}")

        job = wait_for_controller_job(client, queued.job_id, on_event=show_event)
    except ControllerClientError as exc:
        audit(
            root,
            AuditAction.DESTROY,
            actor="cli",
            ok=False,
            detail=f"destroy-infra controller refusal: {type(exc).__name__}",
            duration_s=round(_time.time() - t0, 1),
        )
        raise click.ClickException(str(exc)) from exc
    audit(
        root,
        AuditAction.DESTROY,
        actor="cli",
        ok=job.state.value == "SUCCEEDED",
        detail=f"destroy-infra controller job={job.job_id} state={job.state.value}",
        duration_s=round(_time.time() - t0, 1),
    )
    return job


@deploy.command("destroy-host")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def destroy_host(ctx, yes):
    """Full Proxmox host wipe: guests + ZFS + state + generated files.

    Destroys all provisioned guests via tofu destroy, destroys ZFS pool
    on the Proxmox host, cleans terraform state, and removes generated files.
    """
    import time as _time

    from toolkit.core.infra.infra_destroy import destroy_host_guarded
    from toolkit.core.state.audit_log import AuditAction, audit

    root = Path(ctx.obj["root"])

    if not yes:
        if not click.confirm(
            "DESTROY all homelab guests on Proxmox, wipe ZFS pool, and clean state? This CANNOT be undone.",
            default=False,
        ):
            click.echo("Aborted.")
            return

    started = _time.monotonic()
    try:
        code = destroy_host_guarded(root, on_log=click.echo)
    except RuntimeError as exc:
        audit(
            root,
            AuditAction.DESTROY,
            actor="cli",
            ok=False,
            detail=f"destroy-host refused: {exc}",
            duration_s=_time.monotonic() - started,
        )
        raise click.ClickException(str(exc)) from exc
    audit(
        root,
        AuditAction.DESTROY,
        actor="cli",
        ok=code == 0,
        detail=f"destroy-host exit={code}",
        duration_s=_time.monotonic() - started,
    )
    if code != 0:
        raise click.ClickException(
            "host wipe stopped before local cleanup; inspect the operation log and preserved state"
        )
    click.echo("Host wipe complete. Run 'homelab-toolkit deploy all' to redeploy.")


def _progress_echo(info: dict[str, str], *, err: bool = False) -> None:
    # Phase C: previously this callback was plumbed through the workflow API
    # and then discarded (a no-op). Now it prints the per-step percent + label
    # so long deploys show sub-step progress instead of just step transitions.
    pct = info.get("percent") or info.get("pct") or ""
    label = info.get("step") or info.get("label") or info.get("current_step") or ""
    if pct:
        click.echo(f"  [{pct:>3}%] {label}".rstrip(), err=err)


@deploy.command("all")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable progress and result")
@click.option("--skip-infra", is_flag=True, help="Skip OpenTofu/Ansible infra provisioning")
@click.option("--skip-dns", is_flag=True, help="Skip Cloudflare DNS sync")
@click.option(
    "--destroy-first",
    is_flag=True,
    help="Run tofu destroy before provisioning (clean redeploy)",
)
@click.option("--yes", "-y", is_flag=True, help="Non-interactive (auto-approve destroy when used with --destroy-first)")
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write deploy output to this file (default: .homelab-state/deploy-YYYYmmdd-HHMMSS.log)",
)
@click.option("--node", "vm", default=None, help="Limit deploy to one configured machine ID")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show an offline resource plan from existing config without making changes",
)
@click.pass_context
def deploy_all(ctx, as_json, skip_infra, skip_dns, destroy_first, yes, log_file, vm, dry_run, operation_lease=None):
    """Provision infrastructure, generate configs, deploy services, wire hooks, sync DNS.

    The SINGLE deploy command. A normal deployment auto-detects missing
    config/secrets/generated files and creates them. Dry-runs are read-only.
    """
    import asyncio
    import os
    import time as _time

    _deploy_t0 = _time.monotonic()
    destroy_first_requested = destroy_first

    from toolkit.core.ansible.ansible_inventory import ensure_group_vars_all, write_inventory
    from toolkit.core.config.config import (
        Config,
        DNSConfig,
        NetworkConfig,
        ProxmoxConfig,
        ProxmoxSSHConfig,
        ServicesConfig,
        SSHConfig,
        load_config,
        save_config,
        save_local_config,
    )
    from toolkit.core.config.storage import config_path as get_config_path
    from toolkit.core.config.storage import secrets_path as get_secrets_path
    from toolkit.core.deploy.deploy_log import DeployLogWriter, default_deploy_log_path, should_echo_deploy_line
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow, run_dry_run_workflow, workflow_step_labels
    from toolkit.core.generate.generate import run_full_generate
    from toolkit.core.infra.iac_sync import sync_from_repo_root
    from toolkit.core.secrets.secrets import (
        generate_all_secrets,
        get_required_secrets,
        load_secrets_plaintext,
        save_secrets_plaintext,
    )

    root = Path(ctx.obj["root"])
    cp = get_config_path(root)
    generated_dir = root / "generated"
    _env = os.environ.get

    # A dry-run is a read-only, offline planning operation.  Keep it before
    # bootstrap, generation, and storage discovery so it cannot mutate state
    # or open remote connections merely to render a plan.
    if dry_run:
        if not cp.is_file():
            raise click.ClickException("--dry-run requires an existing config.yaml; run setup or deploy all first")
        cfg = load_config(cp)
        click.echo("")
        result = asyncio.run(
            run_dry_run_workflow(
                root,
                cfg,
                on_log=click.echo,
                targets=(vm,) if vm else None,
            )
        )
        ctx.exit(0 if result.success else 1)
        return

    if destroy_first:
        if not cp.is_file():
            raise click.ClickException("--destroy-first requires an existing configuration")
        # The controller's guarded destroy acquires the operation lease
        # atomically. A local pre-check would only introduce a TOCTOU window.
        destroyed = _destroy_infra_via_controller(ctx, yes=yes)
        if destroyed.state.value != "SUCCEEDED":
            message = destroyed.error.message if destroyed.error else "Infrastructure destruction did not complete"
            raise click.ClickException(message)
        destroy_first = False

    if operation_lease is None:
        from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

        try:
            operation_lease = OperationLease.acquire(root, "deploy")
        except LeaseBusyError as exc:
            message = "Another deployment or mutation is already running"
            if as_json:
                click.echo(json.dumps({"success": False, "error": message}))
            raise click.ClickException(message) from exc
        ctx.call_on_close(operation_lease.release)
    elif _blocking_external_deploy(root, operation_lease) is not None:
        raise click.ClickException("Supplied operation lease does not own the active deployment boundary")

    # ── Step 0: Auto-detect missing config/secrets/generated ────────────
    if not cp.exists():
        if not as_json:
            click.echo("config.yaml missing — auto-creating from environment...")
        domain = _env("HOMELAB_DOMAIN", "localhost")
        email = _env("HOMELAB_EMAIL", "")
        timezone = _env("HOMELAB_TIMEZONE", "UTC")
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        categories = all_categories()
        category_names = {category.name for category in categories}
        services_raw = _env("HOMELAB_SERVICES", ",".join(sorted(category_names)))
        requested_categories = {name.strip() for name in services_raw.split(",") if name.strip()}
        unknown_categories = sorted(requested_categories - category_names)
        if unknown_categories:
            raise click.ClickException(f"Unknown service categories: {', '.join(unknown_categories)}")
        services = ServicesConfig.model_validate(
            {category.name: category.always_on or category.name in requested_categories for category in categories}
        )
        from toolkit.core.manifest.setup import setup_settings_from_environment

        service_settings = setup_settings_from_environment(services, os.environ)
        dns_public_ip = _env("HOMELAB_PUBLIC_IP", "")
        proxmox_config = ProxmoxConfig(
            api_url=_env("PROXMOX_API_URL", ""),
            node=_env("PROXMOX_NODE", "pve"),
            ssh_public_key=_env("PROXMOX_SSH_PUBLIC_KEY", ""),
            ssh=ProxmoxSSHConfig(
                user=_env("PROXMOX_SSH_USER", "root"),
                port=int(_env("PROXMOX_SSH_PORT", "22")),
                key_file=_env("PROXMOX_SSH_KEY_FILE", ""),
            ),
        )
        ssh_config = SSHConfig(key_file=_env("HOMELAB_SSH_KEY_FILE", ""))
        from toolkit.core.machines import load_default_machines

        machines = load_default_machines()
        for machine_id, machine in machines.items():
            prefix = f"HOMELAB_MACHINE_{machine_id.upper().replace('-', '_')}"
            address = _env(f"{prefix}_ADDRESS", machine.address)
            enabled = _env(f"{prefix}_ENABLED", str(machine.enabled)).strip().lower() in ("1", "true", "yes")
            machines[machine_id] = machine.model_copy(update={"address": address, "enabled": enabled})
        cfg = Config(
            domain=domain,
            email=email,
            timezone=timezone,
            services=services,
            service_settings=service_settings,
            dns=DNSConfig(public_ip=dns_public_ip) if dns_public_ip else DNSConfig(),
            proxmox=proxmox_config,
            machines=machines,
            ssh=ssh_config,
            network=NetworkConfig(),
        )
        save_config(cfg, cp)
        if _env("PROXMOX_SSH_PUBLIC_KEY") or _env("PROXMOX_SSH_KEY_FILE") or _env("HOMELAB_SSH_KEY_FILE"):
            save_local_config(cfg, root)
        if not as_json:
            click.echo(f"  Created config.yaml (domain={domain}, services={','.join(cfg.enabled_categories)})")

    sp = get_secrets_path(root)
    existing_secrets = load_secrets_plaintext(sp) if sp.exists() else {}
    has_secrets = sp.exists() and len(existing_secrets) > 3
    if not has_secrets:
        if not as_json:
            click.echo("secrets missing or empty — auto-generating...")
        cfg = load_config(cp)
        if cfg.owner_password:
            existing_secrets["SSO_USER_PASSWORD"] = cfg.owner_password
        specs = get_required_secrets(cfg)
        from toolkit.core.manifest.setup import setup_credentials_from_environment

        supplied = setup_credentials_from_environment(cfg, os.environ)
        for secret_name, fallback_name in [
            ("PROXMOX_API_TOKEN_ID", "TF_VAR_proxmox_api_token_id"),
            ("PROXMOX_API_TOKEN_SECRET", "TF_VAR_proxmox_api_token_secret"),
            ("CLOUDFLARE_API_TOKEN", ""),
            ("CLOUDFLARE_ZONE_ID", ""),
        ]:
            value = _env(secret_name, "") or (_env(fallback_name, "") if fallback_name else "")
            if value:
                supplied[secret_name] = value
        all_secrets = generate_all_secrets(specs, {**existing_secrets, **supplied})
        save_secrets_plaintext(all_secrets, sp)
        if not as_json:
            click.echo(f"  {len(all_secrets)} secrets generated")

    if not generated_dir.exists() or not any(generated_dir.iterdir()):
        if not as_json:
            click.echo("generated files missing — running sync+generate...")
        cfg = load_config(cp)
        has_proxmox = bool(cfg.proxmox.api_url) and "REPLACE_WITH_YOUR" not in (cfg.proxmox.ssh_public_key or "")
        has_infra_dir = (root / "infrastructure").is_dir()
        if has_proxmox and has_infra_dir:
            try:
                sync_from_repo_root(root)
            except ValueError as e:
                if not as_json:
                    click.echo(f"  Sync warn: {e}")
                skip_infra = True
        run_full_generate(root, cfg, validate=True)
        ensure_group_vars_all(root)
        write_inventory(root, cfg)
        if not as_json:
            click.echo("  generated files and inventory ready")

    # Load final config for workflow
    cfg = load_config(cp)

    # ── ZFS auto-detection ──
    storage = getattr(cfg, "storage", None)
    has_storage = storage and (storage.zfs_enabled or storage.raw_disks_gb > 0)
    if not has_storage and cfg.proxmox.provision_machines:
        from toolkit.core.config.config import save_config
        from toolkit.core.infra.zfs_detect import detect_and_merge_zfs

        if not as_json:
            click.echo("No storage config detected — attempting ZFS auto-detection on Proxmox host...")
        cfg, zfs_result, zfs_msg = detect_and_merge_zfs(cfg, root, auto_apply=yes)
        if zfs_result.ok:
            if not as_json:
                click.echo(zfs_msg)
            if yes:
                save_config(cfg, cp)
                if not as_json:
                    click.echo("  Storage config auto-saved to config.yaml")
            elif zfs_result.pools:
                suggested = zfs_result.to_storage_config()
                pool_name = suggested.get("zfs_pool", "unknown")
                raid = suggested.get("raid_level", "unknown")
                gb = suggested.get("raw_disks_gb", 0)
                if not as_json:
                    click.echo(f"  Pool: {pool_name} ({raid}), {gb}GB raw, disks: {suggested.get('disk_count', 0)}")
                    if click.confirm("Apply this ZFS config?", default=True):
                        # Rebuild with detected storage
                        from toolkit.core.config.config import StorageConfig

                        new_storage = StorageConfig(**suggested)
                        cfg = cfg.model_copy(update={"storage": new_storage})
                        save_config(cfg, cp)
                        click.echo("  Storage config saved to config.yaml")
                    else:
                        click.echo("  Skipped ZFS auto-config — using defaults")
        else:
            if not as_json:
                click.echo(f"  {zfs_msg}")

    labels = workflow_step_labels(cfg)
    logs: list[str] = []
    steps: dict[str, str] = {}
    log_path = Path(log_file) if log_file else default_deploy_log_path(root)

    def echo(msg: str) -> None:
        if not as_json:
            click.echo(msg)

    def on_log(msg: str) -> None:
        logs.append(msg)

    def on_step(step: str, status: str) -> None:
        steps[step] = status
        if not as_json:
            _step_echo(step, status, labels)

    if not as_json:
        click.echo(f"Deploy log: {log_path}")

    with DeployLogWriter(log_path) as log_writer:

        def on_log_tee(msg: str) -> None:
            on_log(msg)
            log_writer.write(msg)
            if should_echo_deploy_line(msg):
                echo(msg)

        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=on_log_tee,
                on_step=on_step,
                on_progress=lambda info: _progress_echo(info, err=as_json),
                skip_infra=skip_infra,
                skip_dns=skip_dns,
                targets=(vm,) if vm else None,
                operation_lease=operation_lease,
            )
        )

    # Best-effort unified audit entry for the deploy.
    try:
        from toolkit.core.state.audit_log import AuditAction, audit

        audit(
            root,
            AuditAction.DEPLOY,
            actor="cli",
            ok=result.success,
            detail=result.message[:300] if result.message else ("ok" if result.success else "failed"),
            vm=vm,
            duration_s=round(_time.monotonic() - _deploy_t0, 1) if _deploy_t0 else None,
            extra={
                "destroy_first": destroy_first_requested,
                "skip_infra": skip_infra,
                "target_vm": vm,
                "failed_steps": [s for s, v in steps.items() if v == "failed"],
            },
        )
    except Exception:
        pass

    if as_json:
        payload = {
            "success": result.success,
            "message": result.message,
            "steps": steps,
            "logs": logs,
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        # Formatted recap — the user expects a clear summary, not just one line.
        _print_deploy_recap(result, steps, labels, logs, cfg)

    if not result.success:
        ctx.exit(1)


@deploy.command("recover")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable progress and result")
@click.option("--node", "vm", default=None, help="Limit recovery to one configured machine ID")
@click.pass_context
def deploy_recover(ctx, as_json, vm):
    """Recover deploy: ansible deploy-recover.yml then verify."""
    import asyncio

    from toolkit.cli import load_root_config
    from toolkit.core.deploy.deploy_lock import read_deploy_lock
    from toolkit.core.deploy.deploy_log import DeployLogWriter, default_deploy_log_path, should_echo_deploy_line
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow, workflow_step_labels

    root, cfg = load_root_config(ctx)

    lock = read_deploy_lock(root)
    if lock.blocking:
        if as_json:
            click.echo(json.dumps({"success": False, "message": lock.message, "steps": {}, "logs": []}, indent=2))
        raise click.ClickException(lock.message)

    labels = workflow_step_labels(cfg)
    logs: list[str] = []
    steps: dict[str, str] = {}
    log_path = default_deploy_log_path(root)

    def echo(msg: str) -> None:
        if not as_json:
            click.echo(msg)

    def on_log(msg: str) -> None:
        logs.append(msg)

    def on_step(step: str, status: str) -> None:
        steps[step] = status
        if not as_json:
            _step_echo(step, status, labels)

    if not as_json:
        click.echo(f"Deploy log: {log_path}")

    try:
        with DeployLogWriter(log_path) as log_writer:

            def on_log_tee(msg: str) -> None:
                on_log(msg)
                log_writer.write(msg)
                if should_echo_deploy_line(msg):
                    echo(msg)

            # F5: auto-scope recover to failing VMs when no --node is given.
            # Reads watchdog-state.json's _notify_state/_restart_counts (terminal
            # issues + restart-count-terminal services) to limit the recover
            # blast radius — healthy VMs are skipped. Falls back to "all VMs"
            # (vm=None) when the state file is absent or no failures are recorded.
            effective_vm = vm
            if effective_vm is None:
                try:
                    import json as _json

                    from toolkit.core.manifest.catalog import load_service_catalog
                    from toolkit.core.manifest.placement import service_node_map
                    from toolkit.core.ops.watchdog.recover_policy import select_recover_vms_from_watchdog_state
                    from toolkit.core.state.paths import watchdog_state_path

                    state_path = watchdog_state_path(root)
                    if state_path.exists():
                        state = _json.loads(state_path.read_text(encoding="utf-8"))
                        vm_for_service = service_node_map(cfg, load_service_catalog())
                        failed = select_recover_vms_from_watchdog_state(
                            state,
                            vm_for_service=vm_for_service,
                        )
                        if failed:
                            effective_vm = failed[0] if len(failed) == 1 else None  # multi → all
                            echo(f"Smart-recover: scoping to failing VMs {failed}")
                except Exception:
                    pass  # best-effort; fall back to all-VMs recover

            result = asyncio.run(
                run_recover_workflow(
                    root,
                    cfg,
                    on_log=on_log_tee,
                    on_step=on_step,
                    on_progress=_progress_echo,
                    vm=effective_vm,
                )
            )
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(f"Recover failed: {exc}") from exc

    if as_json:
        payload = {
            "success": result.success,
            "message": result.message,
            "steps": steps,
            "logs": logs,
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        _print_deploy_recap(result, steps, labels, logs, cfg)

    if not result.success:
        raise click.ClickException(result.message)


@deploy.command("up")
@click.option("--node", "vm", required=True, help="Configured machine ID to start")
@click.pass_context
def deploy_up(ctx, vm):
    """Staggered Docker Compose startup for one VM role (internal — called via systemd-run)."""
    from toolkit.cli import load_root_config
    from toolkit.core.deploy.staggered_compose import run_staggered_compose

    root, _cfg = load_root_config(ctx)
    sys.exit(run_staggered_compose(root, vm))


@deploy.command("lock")
@click.option("--cancel", is_flag=True, help="Request cancellation of the active operation")
@click.pass_context
def deploy_lock_cmd(ctx, cancel):
    """Show deployment lease state or request cancellation."""

    from toolkit.cli import load_root_config
    from toolkit.core.deploy.deploy_lock import cancel_active_deploy, read_deploy_lock

    root, _cfg = load_root_config(ctx)

    if cancel:
        status = cancel_active_deploy(root)
        click.echo(status.message)
        if status.blocking:
            return
        return

    status = read_deploy_lock(root)
    click.echo(status.message)
    if status.blocking:
        click.echo("Use 'deploy lock --cancel' to request cancellation.")


@deploy.command("status")
@click.pass_context
def deploy_status(ctx):
    """Show deployment status across all nodes."""
    from toolkit.cli import load_root_config
    from toolkit.core.compose.registry import enabled_categories, load_all
    from toolkit.core.config.storage import env_path

    root, cfg = load_root_config(ctx)
    load_all()

    click.echo(f"{'Node':<10} {'IP':<18} {'.env':<8} {'Services'}")
    click.echo("-" * 55)
    for vm_role in cfg.enabled_nodes:
        ip = cfg.node_ip(vm_role)
        ef = env_path(vm_role, root)
        has_env = "OK" if ef.exists() else "MISSING"
        cats = [category for category in enabled_categories(cfg) if category.runtime_node(cfg) == vm_role]
        svc_count = sum(len(c.services(cfg)) for c in cats)
        click.echo(f"{vm_role:<10} {ip:<18} {has_env:<8} {svc_count} services")


@deploy.command("verify")
@click.option("--node", "vm", default=None, help="Limit verification to one configured machine ID")
@click.option("--url", multiple=True, help="Additional URLs to check")
@click.option("--json", "as_json", is_flag=True)
@click.option("--hooks", is_flag=True, help="Verify hook-configured services (APIs)")
@click.option(
    "--qa",
    "run_qa",
    is_flag=True,
    help=(
        "Run extended QA (containers + hooks + Grafana + Wazuh). "
        "Mutually exclusive with --hooks/--external; "
        "run without flags for the default containers+HTTPS verify."
    ),
)
@click.option("--sso", is_flag=True, help="Verify Authelia OIDC discovery and issuer metadata")
@click.option(
    "--strict",
    is_flag=True,
    help="Require a persisted post-start audit with zero warnings (use with --qa or --hooks)",
)
@click.option("--manual-steps", is_flag=True, help="Show post-deploy manual steps for enabled services")
@click.option(
    "--external",
    is_flag=True,
    help="Probe public endpoints via the real internet path (catches Cloudflare/cert issues)",
)
@click.pass_context
def deploy_verify(ctx, vm, url, as_json, hooks, run_qa, sso, strict, manual_steps, external):
    """Verify containers, HTTPS endpoints, hooks, and QA after deploy."""
    import os

    from toolkit.cli import load_root_config
    from toolkit.core.ops.verify import format_report, verify_all, verify_remote

    root, cfg = load_root_config(ctx)

    if strict and not (run_qa or hooks):
        raise click.UsageError("--strict requires --qa or --hooks")

    if external:
        # Probe public endpoints via the real internet path (DNS → Cloudflare → Caddy).
        from toolkit.core.ops.uptime_probe import run_uptime_probe

        summary = run_uptime_probe(cfg, root)
        click.echo(f"=== External uptime probe: {summary['ok']}/{summary['total']} endpoints reachable ===")
        for r in summary["results"]:
            mark = "✓" if r["ok"] else "✗"
            color = "green" if r["ok"] else "red"
            click.secho(
                f"  {mark} {r['service']:<16} {r['status_code']:<4} {r['latency_ms']:.0f}ms  {r['detail']}", fg=color
            )
        if summary["failed"]:
            ctx.exit(1)
        return

    if run_qa:
        from toolkit.core.deploy.deploy_qa import run_deploy_qa

        logs_qa: list[str] = []

        def on_log_qa(msg: str) -> None:
            if not as_json:
                click.echo(msg)
            logs_qa.append(msg)

        result = run_deploy_qa(root, cfg, vm=vm, strict_hook_audit=strict, on_log=on_log_qa)
        if as_json:
            click.echo(
                json.dumps(
                    {"ok": result.ok, "sections": result.sections, "logs": logs_qa},
                    indent=2,
                )
            )
        if not result.ok:
            ctx.exit(1)
        return

    if hooks:
        from toolkit.core.ops.hook_verify import format_verify_report, verify_hooks
        from toolkit.core.secrets.secrets import load_runtime_secrets

        secrets_dict = load_runtime_secrets(root, role=vm)
        result = verify_hooks(cfg, secrets_dict, root, vm=vm, on_progress=lambda message: click.echo(f"  → {message}"))
        click.echo(format_verify_report(result))
        if strict:
            from toolkit.core.deploy.hook_audit import strict_hooks_passed

            audit_ok, audit_detail = strict_hooks_passed(root)
            click.echo(f"Strict hook audit: {'clean' if audit_ok else 'not clean'} ({audit_detail})")
            if not audit_ok:
                ctx.exit(1)
        # Best-effort audit entry for verify.
        try:
            from toolkit.core.state.audit_log import AuditAction, audit

            audit(
                root,
                AuditAction.VERIFY,
                actor="cli",
                ok=result.all_passed,
                detail=f"{sum(1 for c in result.checks if c.passed)}/{len(result.checks)} passed",
                vm=vm,
                extra={
                    "failed": [f"{c.service}.{c.check}" for c in result.checks if not c.passed][:20],
                },
            )
        except Exception:
            pass
        if not result.all_passed:
            ctx.exit(1)
        return

    if manual_steps:
        from toolkit.core.ops.manual_steps import format_manual_steps_cli, get_all_manual_guidance

        steps = get_all_manual_guidance(cfg)
        if as_json:
            payload = [
                {
                    "service": s.service,
                    "title": s.title,
                    "instructions": s.instructions,
                    "url": s.url,
                    "category": s.category,
                    "hook_failed": s.hook_failed,
                }
                for s in steps
            ]
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(format_manual_steps_cli(steps))
        return

    if sso:
        from toolkit.core.ops.verify import format_sso_report, verify_sso

        report = verify_sso(cfg, root=root)
        if as_json:
            click.echo(json.dumps(report, indent=2))
        else:
            click.echo(format_sso_report(report))
        if not report.get("ok"):
            ctx.exit(1)
        return

    extra = list(url) or None
    from toolkit.core.ansible.ansible_ssh import should_verify_remote

    on_guest = bool(os.environ.get("HOMELAB_NODE"))
    use_remote = should_verify_remote(cfg, root, on_guest=on_guest)
    if use_remote:
        results = verify_remote(root, cfg, vm=vm, extra_urls=extra)
    else:
        results = verify_all(root, cfg, vm=vm, extra_urls=extra)

    from toolkit.core.ops.verify import save_last_verify_report

    save_last_verify_report(root, results)

    if as_json:
        payload = {
            name: {
                "ok": r.ok,
                "healthy": r.services_healthy,
                "unhealthy": r.services_unhealthy,
                "urls": [{"url": u, "ok": ok, "detail": d} for u, ok, d in r.url_checks],
                "errors": r.errors,
            }
            for name, r in results.items()
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(format_report(results))

    if not all(r.ok for r in results.values()):
        ctx.exit(1)


@deploy.command("ssh-test")
@click.pass_context
def deploy_ssh_test(ctx: click.Context):
    """Test SSH to Proxmox jump and each LXC (refreshes known_hosts)."""
    from toolkit.cli import load_root_config
    from toolkit.core.infra.ssh_probe import probe_ssh_connectivity

    root, cfg = load_root_config(ctx)
    lines = probe_ssh_connectivity(cfg, root)
    for line in lines:
        click.echo(line)
    if any("FAIL" in line for line in lines):
        raise SystemExit(1)


@deploy.command("hooks")
@click.option("--node", "vm", default=None, help="Only run hooks for categories on this configured node")
@click.pass_context
def run_hooks(ctx, vm):
    """Run post-start hooks for all services (internal — called by deploy all)."""
    from toolkit.cli import load_root_config
    from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

    root, cfg = load_root_config(ctx)
    if vm and vm not in cfg.enabled_nodes:
        raise click.ClickException(f"Unknown or disabled machine: {vm}")

    from toolkit.core.ansible.ansible_ssh import should_verify_remote

    on_guest = bool(os.environ.get("HOMELAB_NODE"))
    use_remote = should_verify_remote(cfg, root, on_guest=on_guest)

    if use_remote:
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks_remote

        guests = [vm] if vm else list(cfg.enabled_nodes)
        click.echo("Running post-start hooks on guest VM(s) (not on controller)...")
        failed = False
        for guest in guests:
            click.echo(f"\n--- hooks: {guest} ---")
            results, ok = run_post_start_hooks_remote(cfg, root, guest)
            for cat, logs in results.items():
                click.echo(f"\n[{cat}]")
                for line in logs:
                    click.echo(f"  {line}")
                    if line.startswith("Hook error:"):
                        failed = True
            if not ok:
                failed = True
        if failed:
            ctx.exit(1)
        return

    if vm:
        click.echo(f"Running post-start hooks for VM: {vm}...")
    else:
        click.echo("Running post-start hooks...")
    results = run_post_start_hooks(cfg, root, vm=vm, on_progress=click.echo)
    if not results:
        click.echo("No hooks to run.")
        return
    has_errors = False
    for cat, logs in results.items():
        click.echo(f"\n[{cat}]")
        for line in logs:
            click.echo(f"  {line}")
            if line.startswith("Hook error:"):
                has_errors = True
    if has_errors:
        ctx.exit(1)


@deploy.command("reconcile")
@click.option("--apply", is_flag=True, help="Run generate + redeploy drifted VMs when drift is detected")
@click.option("--dry-run", is_flag=True, help="Show what would change without applying")
@click.pass_context
def deploy_reconcile(ctx, apply, dry_run):
    """Detect drift between config intent and live state, optionally self-heal.

    Compares the current discovery snapshot (services, VMs, domains from
    config.yaml + docker-compose.yml) against the last recorded fingerprint
    in ``.homelab-state/last-reconcile.json``. Drift happens when someone edits
    config.yaml, toggles categories, or manually adds/removes containers
    without re-running generate.

    With ``--apply``: runs ``generate`` then ``deploy up --node <drifted>`` for
    each VM whose service set changed.
    """
    import time as _time

    from toolkit.cli import load_root_config
    from toolkit.core.registry.reconcile import build_discovery_snapshot
    from toolkit.core.state.audit_log import AuditAction, audit

    root, cfg = load_root_config(ctx)
    t0 = _time.monotonic()

    state_path = root / ".homelab-state" / "last-reconcile.json"
    current = build_discovery_snapshot(cfg, root)

    previous = {}
    prev_fp = ""
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text())
            prev_fp = str(previous.get("desired_fingerprint") or "")
        except (json.JSONDecodeError, OSError):
            pass

    drift = []
    if prev_fp:
        prev_discovery = previous.get("discovery") or {}
        for key, value in current.items():
            if prev_discovery.get(key) != value:
                drift.append(key)
    else:
        drift.append("(no previous fingerprint)")

    drifted = bool(drift)
    if not drifted:
        click.secho("✓ No drift — config and live state match the last reconcile fingerprint", fg="green")
        audit(
            root,
            AuditAction.RECONCILE,
            actor="cli",
            ok=True,
            detail="no drift",
            duration_s=round(_time.monotonic() - t0, 1),
        )
        return

    click.secho(f"⚠ Drift detected ({len(drift)} field(s) changed):", fg="yellow")
    for field in drift:
        old_val = (previous.get("discovery") or {}).get(field)
        new_val = current.get(field)
        click.echo(f"  {field}: {old_val!r} → {new_val!r}")

    if dry_run or not apply:
        click.echo("\nRun with --apply to run generate + redeploy drifted VMs.")
        audit(
            root,
            AuditAction.RECONCILE,
            actor="cli",
            ok=False,
            detail=f"drift in {', '.join(drift[:5])}",
            duration_s=round(_time.monotonic() - t0, 1),
        )
        return

    # --apply: run generate, then redeploy drifted VMs.
    click.echo("\n→ Running generate...")
    from toolkit.cli.generate_cmd import generate

    ctx.invoke(generate)

    # Determine drifted VMs from the discovery change.
    old_vms = set((previous.get("discovery") or {}).get("enabled_nodes") or [])
    new_vms = set(current.get("enabled_nodes") or [])
    drifted_vms = sorted(new_vms - old_vms)
    if not drifted_vms:
        # If VM list didn't change, redeploy all enabled VMs (service set may have changed).
        drifted_vms = sorted(current.get("enabled_nodes") or [])
    click.echo(f"\n→ Re-deploying drifted VM(s): {', '.join(drifted_vms)}")
    from toolkit.core.deploy.staggered_compose import run_staggered_compose

    for vm in drifted_vms:
        click.echo(f"\n--- {vm} ---")
        rc = run_staggered_compose(root, vm)
        if rc != 0:
            click.secho(f"  ✗ {vm} deploy failed (rc={rc})", fg="red")
    audit(
        root,
        AuditAction.RECONCILE,
        actor="cli",
        ok=True,
        detail=f"applied: generate + redeploy {','.join(drifted_vms)}",
        duration_s=round(_time.monotonic() - t0, 1),
    )
