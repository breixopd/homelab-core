from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from toolkit.cli import load_root_config


@click.group()
def watchdog():
    """Monitor services and auto-recover safe-to-restart issues."""


def _is_cert_image_issue(issue) -> bool:
    """Check if a HealthIssue relates to SSL certificates or image staleness."""
    msg = issue.message.lower()
    return "ssl certificate" in msg or "certificate for" in msg or "days old" in msg or "image" in msg


@watchdog.command("check")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--certificates", is_flag=True, help="Show only SSL certificate and image staleness checks")
@click.pass_context
def check(ctx, as_json: bool, certificates: bool):
    """Run a full health scan of all containers and system resources."""
    root, cfg = load_root_config(ctx)

    from toolkit.core.ops.watchdog import Watchdog

    wd = Watchdog(root, cfg)
    report = wd.full_check()

    if as_json:
        import json

        click.echo(json.dumps(report.to_dict(), indent=2))
    elif certificates:
        cert_issues = [i for i in report.issues if _is_cert_image_issue(i)]
        click.echo("=== Certificate & Image Expiry ===")
        if cert_issues:
            for issue in cert_issues:
                color = "red" if issue.severity == "critical" else "yellow"
                click.secho(f"  [{issue.severity}] {issue.service}: {issue.message}", fg=color)
                if issue.diagnosis:
                    click.secho(f"    💡 {issue.diagnosis}", fg="cyan")
        else:
            click.secho("  No certificate or image issues detected.", fg="green")
        if cert_issues:
            ctx.exit(1)
        else:
            ctx.exit(0)
    else:
        click.echo(f"Health: {report.summary()}")
        if report.healthy:
            click.secho(f"  ✓ {len(report.healthy)} containers healthy", fg="green")
        for issue in report.issues:
            color = "red" if issue.severity == "critical" else "yellow"
            fix = " [auto-fixable]" if issue.auto_fixable else ""
            click.secho(f"  ✗ {issue.service}: {issue.message}{fix}", fg=color)
            if issue.diagnosis:
                click.secho(f"    💡 {issue.diagnosis}", fg="cyan")
    if report.issues and not certificates:
        ctx.exit(1)


@watchdog.command("heal")
@click.option("--dry-run", is_flag=True, help="Show what would be done without doing it")
@click.option("--notify/--no-notify", default=True, help="Send notifications after healing")
@click.pass_context
def heal(ctx, dry_run: bool, notify: bool):
    """Attempt to auto-fix any detected issues."""
    import time

    from toolkit.core.state.audit_log import AuditAction, audit

    root, cfg = load_root_config(ctx)

    from toolkit.core.ops.watchdog import HealResult, Watchdog

    wd = Watchdog(root, cfg)
    t0 = time.time()
    report = wd.full_check()

    fixable = [i for i in report.issues if i.auto_fixable]
    result = HealResult()
    if fixable:
        if dry_run:
            click.echo(f"Would attempt to fix {len(fixable)} issue(s):")
            for issue in fixable:
                click.echo(f"  - Restart {issue.service}: {issue.message}")
                if issue.diagnosis:
                    click.echo(f"    💡 {issue.diagnosis}")
        else:
            result = wd.heal(report)
            for log in result.logs:
                click.echo(log)
            click.echo(f"Outcome: {result.succeeded} succeeded, {result.failed} failed, {result.deferred} deferred")
    elif report.issues:
        click.echo(f"No auto-fixable issues ({len(report.issues)} need attention).")

    # Always notify when there are issues (not just fixable ones) so the
    # systemd timer surfaces non-fixable critical alerts via ntfy.
    notified: list[str] = []
    if notify and report.issues:
        notified = wd.notify(report)
        for msg in notified:
            click.echo(f"  NOTIFY: {msg}")

    # Best-effort audit entry for every heal run (success or not).
    audit(
        root,
        AuditAction.HEAL,
        actor="watchdog-timer" if os.environ.get("INVOCATION_ID") else "cli",
        ok=result.ok,
        detail=(
            f"{len(report.issues)} issue(s), {result.succeeded} succeeded, "
            f"{result.failed} failed, {result.deferred} deferred, {len(notified)} notified"
        ),
        duration_s=round(time.time() - t0, 1),
        extra={
            "critical": sum(1 for i in report.issues if i.severity == "critical"),
            "attempted": result.attempted,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "deferred": result.deferred,
            "dry_run": dry_run,
        },
    )
    if result.failed:
        raise click.ClickException(f"{result.failed} healing remedy failed")


@watchdog.command("rightsize")
@click.option("--node", "vm", default=None, help="Scope to one configured node (default: all enabled).")
@click.option("--apply", "do_apply", is_flag=True, help="Auto-apply safe proposals (default: dry-run only).")
@click.option("--dry-run", is_flag=True, help="Show proposals without applying (default).")
@click.option("--json", "as_json", is_flag=True, help="Output proposals as JSON.")
@click.pass_context
def rightsize(ctx, vm, do_apply, dry_run, as_json):
    """Compute and (optionally) apply container resource rightsizing proposals.

    Queries node-scoped cAdvisor metrics for p95 demand and the limits enforced
    by Docker, using the policy configured in the management service settings.

    Bounded stateless reductions are deployed and verified with --apply. Growth
    and stateful changes require explicit approval and use the same rollback path.
    """
    if do_apply and (dry_run or as_json):
        raise click.UsageError("--apply cannot be combined with --dry-run or --json")

    from toolkit.core.ops.watchdog.rightsize import (
        apply_rightsize_proposals,
        compute_rightsize_proposals,
        reconcile_rightsize_nodes,
        rightsize_config_from_desired_state,
    )

    root, cfg = load_root_config(ctx)
    if vm and vm not in cfg.enabled_nodes:
        raise click.ClickException(f"machine {vm!r} is not enabled")
    target_vms = [vm] if vm else cfg.enabled_nodes
    policy = rightsize_config_from_desired_state(cfg)
    all_proposals: list = []
    for one_vm in target_vms:
        all_proposals.extend(compute_rightsize_proposals(vm=one_vm, root=root, cfg=policy))

    if not all_proposals:
        click.echo("No rightsizing proposals (insufficient telemetry or no change needed).")
        return

    if as_json:
        import json as _json

        click.echo(
            _json.dumps(
                [
                    {
                        "vm": p.vm,
                        "service": p.service,
                        "current_mem_mb": p.current_mem_mb,
                        "proposed_mem_mb": p.proposed_mem_mb,
                        "current_cpus": p.current_cpus,
                        "proposed_cpus": p.proposed_cpus,
                        "p95_mem_mb": p.p95_mem_mb,
                        "p95_cpu_pct": p.p95_cpu_pct,
                        "reason": p.reason,
                        "safe_to_apply": p.safe_to_apply,
                        "stateful": p.stateful,
                        "blocked_reason": p.blocked_reason,
                        "change_pct": round(p.change_pct, 1),
                    }
                    for p in all_proposals
                ],
                indent=2,
            )
        )
        return

    for p in all_proposals:
        flag = "APPLY" if p.safe_to_apply else "PROPOSE"
        click.secho(
            f"  [{flag:6}] {p.vm}/{p.service:<25} "
            f"mem {p.current_mem_mb}→{p.proposed_mem_mb} MB "
            f"cpus {p.current_cpus}→{p.proposed_cpus} "
            f"(p95 mem {p.p95_mem_mb:.0f}MB cpu {p.p95_cpu_pct:.0f}%) "
            f"— {p.reason}" + (f" [{p.blocked_reason}]" if p.blocked_reason else ""),
            fg="green" if p.safe_to_apply else "yellow",
        )

    if do_apply:
        guarded = [
            proposal
            for proposal in all_proposals
            if not proposal.safe_to_apply and proposal.blocked_reason in {"stateful-service", "capacity-growth"}
        ]
        deferred = [
            proposal
            for proposal in all_proposals
            if not proposal.safe_to_apply and proposal.blocked_reason not in {"stateful-service", "capacity-growth"}
        ]
        store = None
        existing: set[tuple[str, str]] = set()
        if guarded:
            from toolkit.core.ops.approvals import ApprovalKind, ApprovalPersistenceError, ApprovalStore

            try:
                store = ApprovalStore(root=root)
                existing = {
                    (str(approval.payload.get("node", "")), approval.service)
                    for approval in store.actionable()
                    if approval.kind is ApprovalKind.RIGHTSIZE
                }
            except ApprovalPersistenceError as exc:
                raise click.ClickException(str(exc)) from exc
        try:
            applied = apply_rightsize_proposals(
                [p for p in all_proposals if p.safe_to_apply],
                root=root,
                reconcile=lambda desired, nodes: reconcile_rightsize_nodes(
                    root,
                    desired,
                    nodes,
                    on_log=lambda message: click.echo(f"  {message}"),
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        enqueued = 0
        if guarded and store is not None:
            from toolkit.core.ops.approvals import ApprovalKind, ApprovalPersistenceError
            from toolkit.core.ops.watchdog.rightsize import RightsizeApprovalPayload

            try:
                for p in guarded:
                    if (p.vm, p.service) in existing:
                        continue
                    store.enqueue(
                        ApprovalKind.RIGHTSIZE,
                        p.service,
                        f"{p.current_mem_mb} MB / {p.current_cpus:g} CPU",
                        f"{p.proposed_mem_mb} MB / {p.proposed_cpus:g} CPU",
                        reason=p.reason,
                        requested_by="watchdog-rightsize",
                        payload=RightsizeApprovalPayload.from_proposal(p).model_dump(mode="json"),
                    )
                    enqueued += 1
            except ApprovalPersistenceError as exc:
                raise click.ClickException(str(exc)) from exc
        click.secho(
            f"Applied and verified {len(applied)} safe proposal(s); "
            f"{enqueued} guarded proposal(s) enqueued for approval; "
            f"{len(deferred)} proposal(s) deferred by policy "
            f"('approvals list' to review guarded work).",
            fg="green",
        )


@watchdog.command("notify")
@click.pass_context
def notify_cmd(ctx):
    """Send a health report notification via ntfy."""
    root, cfg = load_root_config(ctx)

    from toolkit.core.ops.watchdog import Watchdog

    wd = Watchdog(root, cfg)
    report = wd.full_check()
    click.echo(f"Health: {report.summary()}")
    msgs = wd.notify(report)
    for msg in msgs:
        click.echo(f"  {msg}")


@watchdog.command("daemon")
@click.option("--interval", type=int, default=60, help="Check interval in seconds")
@click.option("--dry-run", is_flag=True, help="Show what would happen without running")
@click.pass_context
def watchdog_daemon(ctx, interval, dry_run):
    """Run watchdog as a background monitoring service.

    Continuously checks container health at the specified interval.

    Docker label auto-discovery: containers with homelab.watchdog.restart-policy
    and homelab.watchdog.depends-on labels are automatically merged into the
    restart safety and dependency maps at each check cycle.
    """
    import time

    root, cfg = load_root_config(ctx)

    from toolkit.core.ops.watchdog import Watchdog

    wd = Watchdog(root, cfg)

    click.echo(f"Watchdog daemon mode: checking every {interval}s (dry-run={dry_run})")
    click.echo("Press Ctrl+C to stop")

    checks = 0
    try:
        while True:
            checks += 1
            click.echo(f"\n[Check #{checks}]")
            report = wd.full_check()
            click.echo(f"  {report.summary()}")
            if report.issues:
                label_summary = (
                    f"  Docker labels: "
                    f"{len(wd._discovered_safe)} safe, "
                    f"{len(wd._discovered_careful)} careful, "
                    f"{len(wd._discovered_deps)} dep maps"
                )
                click.echo(label_summary)
                for issue in report.issues:
                    sev = issue.severity
                    click.echo(f"  [{sev}] {issue.service}: {issue.message}")
                if not dry_run:
                    wd.heal(report)
                    wd.notify(report)
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo(f"\nWatchdog daemon stopped after {checks} checks")


_SYSTEMD_TIMER_TEMPLATE = """[Unit]
Description=Homelab watchdog — health check + auto-heal
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={toolkit} --root {root} watchdog heal --notify
# Prevent overlapping runs if a check takes longer than the timer interval
TimeoutStartSec=10min
# Journald retention is handled by the system; keep logs searchable
StandardOutput=journal
StandardError=journal
"""

_SYSTEMD_TIMER_UNIT = """[Unit]
Description=Run homelab watchdog every {interval}min

[Timer]
OnBootSec=3min
OnUnitActiveSec={interval}min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""


@watchdog.command("install-timer")
@click.option("--interval", type=int, default=5, help="Check interval in minutes (default 5)")
@click.option("--user", is_flag=True, help="Install as a user unit (no sudo needed)")
@click.pass_context
def install_timer(ctx, interval: int, user: bool):
    """Install a systemd timer that runs `watchdog heal` automatically.

    The timer self-installs on the controller (the machine running this CLI).
    It runs `watchdog heal --notify` every N minutes: scans container health,
    auto-restarts safe-to-fix issues, and dispatches ntfy alerts for the rest.
    Idempotent — re-running updates the interval.
    """
    import os
    import shutil
    import subprocess

    root, _ = load_root_config(ctx)
    toolkit_bin = shutil.which("homelab-toolkit") or os.path.abspath(sys.argv[0])

    svc_name = "homelab-watchdog"
    if user:
        unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    else:
        unit_dir = Path("/etc/systemd/system")

    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path = unit_dir / f"{svc_name}.service"
    timer_path = unit_dir / f"{svc_name}.timer"

    service_path.write_text(
        _SYSTEMD_TIMER_TEMPLATE.format(toolkit=toolkit_bin, root=root),
        encoding="utf-8",
    )
    timer_path.write_text(
        _SYSTEMD_TIMER_UNIT.format(interval=interval),
        encoding="utf-8",
    )

    systemctl = ["systemctl", "--user"] if user else ["systemctl"]
    daemon_reload = [*systemctl, "daemon-reload"]
    enable_timer = [*systemctl, "enable", "--now", f"{svc_name}.timer"]

    for cmd in (daemon_reload, enable_timer):
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            click.secho(f"Failed: {' '.join(cmd)}", fg="red")
            click.secho(result.stderr, fg="red")
            ctx.exit(1)

    click.secho(f"✓ Installed {svc_name}.timer (every {interval}min)", fg="green")
    click.secho(f"  Service: {service_path}", fg="cyan")
    click.secho(f"  Timer:   {timer_path}", fg="cyan")
    click.echo("\nNext steps:")
    systemctl_display = " ".join(systemctl)
    click.echo(f"  Check status:  {systemctl_display} status {svc_name}.timer")
    click.echo(f"  View runs:     journalctl -u {svc_name}.service -f")
    click.echo(f"  Uninstall:     homelab-toolkit watchdog uninstall-timer{' --user' if user else ''}")


@watchdog.command("uninstall-timer")
@click.option("--user", is_flag=True, help="Uninstall a user unit")
@click.pass_context
def uninstall_timer(ctx, user: bool):
    """Remove the watchdog systemd timer."""
    import os
    import subprocess

    svc_name = "homelab-watchdog"
    if user:
        unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
        systemctl = ["systemctl", "--user"]
    else:
        unit_dir = Path("/etc/systemd/system")
        systemctl = ["systemctl"]

    subprocess.run([*systemctl, "disable", "--now", f"{svc_name}.timer"], capture_output=True, check=False)
    for f in (unit_dir / f"{svc_name}.timer", unit_dir / f"{svc_name}.service"):
        if f.exists():
            f.unlink()
    subprocess.run([*systemctl, "daemon-reload"], capture_output=True, check=False)
    click.secho(f"✓ Removed {svc_name}.timer", fg="green")


@watchdog.command("history")
@click.option("--limit", type=int, default=20, help="Number of entries to show")
@click.option("--action", type=str, default=None, help="Filter by action (deploy, heal, verify...)")
@click.pass_context
def history_cmd(ctx, limit: int, action: str | None):
    """Show recent audit log entries."""
    from datetime import datetime

    from toolkit.core.state.audit_log import read_audit

    root, _ = load_root_config(ctx)
    entries = read_audit(root, action=action, limit=limit)
    if not entries:
        click.echo("No audit entries yet.")
        return
    click.echo(f"=== Last {len(entries)} audit entries ===")
    for e in entries[-limit:]:
        ts = datetime.fromtimestamp(e.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
        ok = "✓" if e.get("ok") else "✗"
        vm = f"[{e['vm']}]" if e.get("vm") else ""
        detail = e.get("detail", "")
        click.secho(f"  {ts} {ok} {e.get('action', '?'):<14} {vm:<8} {detail}", fg=None if e.get("ok") else "red")
