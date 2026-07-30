"""Top-level one-view cluster state.

Fuses the scattered status slices (ops + deploy status + services status +
watchdog summary + last audit row) into a single view so the operator gets the
whole cluster at a glance instead of running 5 separate commands.

Plain-text by default (stable for pipes + tests); --json for machine-readable.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from toolkit.cli import load_root_config
from toolkit.cli._format import echo_panel, echo_table


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit a structured cluster snapshot as JSON.")
@click.pass_context
def status_cmd(ctx, as_json: bool):
    """Show the full cluster state in one view."""
    root, cfg = load_root_config(ctx)

    snapshot = _build_snapshot(root, cfg)

    if as_json:
        click.echo(json.dumps(snapshot, indent=2, default=str))
        return

    # Header panel.
    echo_panel(
        title=f"Cluster: {snapshot['domain']}",
        body=(
            f"Proxmox node: {snapshot['proxmox']['node']}\n"
            f"Public IP:    {snapshot['proxmox']['public_ip']}\n"
            f"Multi-node:    {snapshot['multi_node']}\n"
            f"Enabled nodes: {', '.join(snapshot['enabled_nodes']) or 'none'}"
        ),
    )

    # Machine inventory table.
    vm_rows = [(vm, info["ip"], info["enabled"]) for vm, info in snapshot["vms"].items()]
    if vm_rows:
        echo_table(vm_rows, columns=("Node", "IP", "Enabled"))

    # Watchdog summary.
    wd = snapshot["watchdog"]
    click.echo("")
    click.secho(f"Watchdog: {wd['health']}", fg=_health_color(wd["health"]))
    if wd.get("last_event"):
        click.echo(f"  last event: {wd['last_event']}")
    if wd["issues"]:
        click.echo(f"  open issues: {len(wd['issues'])}")
        for issue in wd["issues"][:5]:
            click.echo(f"    - {issue}")

    if snapshot.get("approvals"):
        click.echo("")
        click.secho(f"Actionable approvals: {len(snapshot['approvals'])}", fg="yellow")
        for a in snapshot["approvals"][:5]:
            click.echo(f"    - [{a['kind']}] {a['service']} {a['current']}→{a['proposed']}")
    if snapshot.get("approval_error"):
        click.secho(f"Approval state error: {snapshot['approval_error']}", fg="red")

    # Last audit row.
    if snapshot.get("last_audit"):
        la = snapshot["last_audit"]
        click.echo("")
        click.echo(f"Last audit: {la['action']} ok={la['ok']} ({la.get('actor', '')}) — {la.get('detail', '')}")


def _health_color(health: str) -> str:
    if health == "healthy":
        return "green"
    if health == "degraded":
        return "yellow"
    return "red"


def _build_snapshot(root: Path, cfg) -> dict:
    """Assemble the cluster snapshot from local state files (no live SSH)."""

    # Machine inventory from config.
    vms: dict[str, dict] = {}
    for vm in cfg.enabled_nodes:
        try:
            vms[vm] = {"ip": cfg.node_ip(vm), "enabled": True}
        except Exception:
            vms[vm] = {"ip": "", "enabled": True}

    # Watchdog summary from watchdog-state.json (if present).
    watchdog_summary = {"health": "unknown", "issues": [], "last_event": ""}
    try:
        from toolkit.core.state.paths import watchdog_state_path

        state_path = watchdog_state_path(root)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            notify_state = state.get("notify_state", {}) or {}
            terminal = sum(1 for e in notify_state.values() if isinstance(e, dict) and e.get("terminal"))
            critical = sum(1 for e in notify_state.values() if isinstance(e, dict) and e.get("severity") == "critical")
            if critical or terminal:
                watchdog_summary["health"] = "degraded" if not terminal else "down"
            else:
                watchdog_summary["health"] = "healthy"
            watchdog_summary["issues"] = [
                e.get("severity", "info") + ":" + k for k, e in list(notify_state.items())[:10] if isinstance(e, dict)
            ]
    except (OSError, json.JSONDecodeError):
        pass

    # Recent events (last watchdog-event line).
    try:
        from toolkit.core.state.paths import watchdog_events_path

        events_path = watchdog_events_path(root)
        if events_path.exists():
            lines = events_path.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1])
                watchdog_summary["last_event"] = (
                    f"{last.get('type', '?')} {last.get('service', '')} {last.get('action', '')}".strip()
                )
    except (OSError, json.JSONDecodeError):
        pass

    approvals: list[dict] = []
    approval_error = ""
    try:
        from toolkit.core.ops.approvals import ApprovalPersistenceError, ApprovalStore

        store = ApprovalStore(root=root)
        approvals = [approval.to_dict() for approval in store.actionable()]
    except ApprovalPersistenceError as exc:
        approval_error = str(exc)

    # Last audit row.
    last_audit: dict = {}
    try:
        from toolkit.core.state.audit_log import read_audit

        entries = read_audit(root, limit=1)
        if entries:
            last_audit = entries[-1]
    except Exception:
        pass

    return {
        "domain": cfg.domain,
        "proxmox": {
            "node": cfg.proxmox.node,
            "api_url": cfg.proxmox.api_url,
            "public_ip": cfg.dns.public_ip or "",
        },
        "multi_node": cfg.is_multi_node,
        "enabled_nodes": list(cfg.enabled_nodes),
        "vms": vms,
        "watchdog": watchdog_summary,
        "approvals": approvals,
        "approval_error": approval_error,
        "last_audit": last_audit,
    }
