from __future__ import annotations

from pathlib import Path

import click


def _get_cf_client(root: Path):
    """Load config + secrets, create authenticated CloudflareDNS client. Returns (cfg, client)."""
    from toolkit.core.ops.dns import cloudflare_client_from_root

    try:
        return cloudflare_client_from_root(root)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def dns():
    """Manage DNS records via Cloudflare."""
    pass


@dns.command("sync")
@click.option("--dry-run", is_flag=True, help="Show what would change without applying")
@click.option("--ip", "public_ip", default=None, help="Public IP (auto-detected if not set)")
@click.option("--list", "list_only", is_flag=True, help="List current Cloudflare DNS records (read-only)")
@click.pass_context
def sync(ctx, dry_run, public_ip, list_only):
    """Sync DNS records from config to Cloudflare (or list current records)."""
    import time as _time

    from toolkit.core.state.audit_log import AuditAction, audit

    root = Path(ctx.obj["root"])
    from toolkit.core.ops.dns import desired_records_from_config, resolve_public_dns_ip, sync_cloudflare_dns

    if list_only:
        cfg, client = _get_cf_client(root)
        records = client.list_records("A") + client.list_records("CNAME")
        click.echo(f"{'Type':<6} {'Name':<40} {'Content':<20} {'Proxied'}")
        click.echo("-" * 75)
        for r in sorted(records, key=lambda x: x.name):
            click.echo(f"{r.type:<6} {r.name:<40} {r.content:<20} {'yes' if r.proxied else 'no'}")
        return

    t0 = _time.time()
    try:
        stats = sync_cloudflare_dns(root, dry_run=dry_run, public_ip=public_ip, on_log=click.echo)
        audit(
            root,
            AuditAction.SYNC_DNS,
            actor="cli",
            ok=True,
            detail=(
                f"{stats.get('created', 0)} created, {stats.get('updated', 0)} updated, "
                f"{stats.get('unchanged', 0)} unchanged"
            ),
            duration_s=round(_time.time() - t0, 1),
        )
    except ValueError as exc:
        audit(
            root,
            AuditAction.SYNC_DNS,
            actor="cli",
            ok=False,
            detail=str(exc)[:200],
            duration_s=round(_time.time() - t0, 1),
        )
        raise click.ClickException(str(exc)) from exc

    if dry_run and stats.get("dry_run"):
        cfg, _ = _get_cf_client(root)
        from toolkit.core.ops.dns import desired_records_from_config, resolve_public_dns_ip

        ip, _ = resolve_public_dns_ip(cfg, public_ip)
        click.echo("\nDry run — no changes applied:")
        for r in desired_records_from_config(cfg, ip or "0.0.0.0"):
            click.echo(f"  {r.type:5} {r.name} → {r.content}")
        return

    click.echo(
        f"\nSync complete: {stats.get('created', 0)} created, {stats.get('updated', 0)} updated, "
        f"{stats.get('unchanged', 0)} unchanged"
    )


@dns.command("cleanup")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def cleanup(ctx, dry_run):
    """Remove DNS records no longer in config."""
    root = Path(ctx.obj["root"])

    cfg, client = _get_cf_client(root)

    if dry_run:
        from toolkit.core.ops.dns import desired_records_from_config

        existing = client.list_records("A") + client.list_records("CNAME")
        desired = desired_records_from_config(cfg, "dummy")
        desired_names = {r.name for r in desired}
        stale = [
            r
            for r in existing
            if r.name.endswith(f".{cfg.domain}") and r.name not in desired_names and r.name != cfg.domain
        ]
        click.echo(f"Found {len(stale)} stale records (dry run):")
        for r in stale:
            click.echo(f"  {r.type} {r.name} → {r.content}")
        return

    from toolkit.core.ops.dns import cleanup_stale_homelab_dns

    deleted = cleanup_stale_homelab_dns(root, on_log=click.echo)
    if not deleted:
        click.echo("No stale records found.")
    else:
        click.echo(f"\nDeleted {deleted} records.")
