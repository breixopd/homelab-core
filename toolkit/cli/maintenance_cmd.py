from __future__ import annotations

import click

from toolkit.cli import load_root_config


@click.group()
def maintenance():
    """DB safety snapshots, uptime probes, Vaultwarden sync, and cleanup."""


@maintenance.command("run")
@click.option("--node", "vm", help="Configured machine ID for the state record")
@click.option("--notify/--no-notify", default=True, help="Send ntfy attention alerts for failures and notices")
@click.pass_context
def run_cmd(ctx, vm: str | None, notify: bool):
    """Run full maintenance on this host."""
    root, _cfg = load_root_config(ctx)
    from toolkit.core.ops.maintenance import run_maintenance

    result = run_maintenance(root, vm=vm, notify_on_attention=notify)
    for line in result.actions:
        click.echo(line)
    for err in result.errors:
        click.secho(err, fg="red", err=True)
    if not result.ok:
        raise click.ClickException("maintenance completed with errors")


@maintenance.command("metrics")
@click.pass_context
def metrics_cmd(ctx):
    """Print Prometheus text metrics for maintenance/disk."""
    root, _cfg = load_root_config(ctx)
    from toolkit.core.ops.maintenance import prometheus_metrics

    click.echo(prometheus_metrics(root), nl=False)


@maintenance.command("snapshot")
@click.option("--node", "role", required=True, help="Configured machine ID")
@click.pass_context
def snapshot_cmd(ctx, role: str):
    """Create and verify an encrypted snapshot for this node."""
    import os
    import shlex
    from typing import cast

    from toolkit.core.manifest.schema import NodeId

    root, cfg = load_root_config(ctx)
    if not cfg.backups.enabled:
        raise click.ClickException("backups are disabled in desired state")
    if role not in cfg.enabled_nodes:
        raise click.ClickException(f"machine {role!r} is not enabled")
    click.echo(f"Backup [{role}]: verifying repository connection...")
    if cfg.is_multi_node and os.environ.get("HOMELAB_NODE") != role:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        command = shlex.join(
            [
                "env",
                f"HOMELAB_NODE={role}",
                "/opt/homelab/.venv/bin/python3",
                "-m",
                "toolkit.cli",
                "--root",
                "/opt/homelab",
                "maintenance",
                "snapshot",
                "--node",
                role,
            ]
        )
        rc, output, error = ssh_run_on_vm(
            cfg,
            cfg.node_ip(role),
            command,
            root=root,
            timeout=3600,
        )
        if output:
            click.echo(output.rstrip())
        if rc != 0:
            detail = error.strip().splitlines()[-1] if error.strip() else ""
            suffix = f": {detail}" if detail else ""
            raise click.ClickException(f"remote snapshot failed on {role}{suffix}")
        return

    from toolkit.core.ops.backups import run_node_snapshot

    result = run_node_snapshot(root, cast(NodeId, role), actor="cli")
    for action in result.actions:
        click.echo(f"  {action}")
    if not result.ok:
        raise click.ClickException(result.message)
    click.secho(result.message, fg="green")


@maintenance.command("dump")
@click.pass_context
def backup_dump(ctx):
    """Take a pre-deploy Postgres dump NOW (manual safety snapshot)."""
    from toolkit.core.ops.db_safety import pre_deploy_dump

    root, cfg = load_root_config(ctx)
    path = pre_deploy_dump(cfg, root)
    if path:
        click.secho(f"✓ Dump: {path}", fg="green")
    else:
        raise click.ClickException("dump failed: postgres is not ready or unreachable")


@maintenance.command("backup-drill")
@click.pass_context
def backup_drill_cmd(ctx):
    """Restore bounded content from every latest Kopia snapshot."""
    from toolkit.core.ops.backup_restore_drill import run_backup_restore_drill

    root, cfg = load_root_config(ctx)
    if not cfg.backups.enabled:
        raise click.ClickException("backups are disabled in desired state")
    click.echo("Backup drill: restoring bounded snapshot evidence...")
    result = run_backup_restore_drill(cfg, root, actor="cli")
    for node in result.nodes:
        if node.ok and node.artifact_count:
            click.echo(f"  {node.role}: verified {node.artifact_count} logical artifact(s)")
        elif node.ok:
            click.echo(f"  {node.role}: verified snapshot content")
        else:
            click.secho(f"  {node.role}: {node.error}", fg="red", err=True)
    if not result.ok:
        raise click.ClickException("backup restore drill failed; inspect the audit log")
    click.secho("Backup restore drill completed", fg="green")


@maintenance.command("restore-db")
@click.argument("dump_id", required=False)
@click.option("--confirm-dump-id", help="Exact dump ID confirmation for non-interactive use")
@click.pass_context
def restore_db(ctx, dump_id: str | None, confirm_dump_id: str | None):
    """Restore a discovered Postgres dump by opaque ID."""
    from toolkit.core.ops.db_safety import list_dumps, restore_dump

    root, cfg = load_root_config(ctx)
    dumps = list_dumps(cfg, root)
    if not dump_id:
        if not dumps:
            click.echo("No pre-deploy dumps found.")
            return
        click.echo("Available dumps (newest first):")
        for d in dumps:
            click.echo(f"  {d.dump_id:<25} {d.name:<35} {d.size:<10}")
        click.echo("\nRun: homelab-toolkit maintenance restore-db <dump-id>")
        return
    try:
        record = next(record for record in dumps if record.dump_id == dump_id)
    except StopIteration as exc:
        raise click.ClickException("dump ID is unknown, changed, or no longer available") from exc
    click.secho(f"This will DROP and REPLACE all Postgres data with {record.name}", fg="yellow")
    confirmation = confirm_dump_id or click.prompt("Type the dump ID to confirm")
    if confirmation != record.dump_id:
        raise click.ClickException("dump ID confirmation did not match")
    ok = restore_dump(cfg, root, record, actor="cli")
    if ok:
        click.secho(f"Restored from {record.name} ({record.dump_id})", fg="green")
    else:
        raise click.ClickException("restore failed; inspect the audit log and restore intent")


@maintenance.command("list-dumps")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable dump metadata")
@click.pass_context
def list_dumps_cmd(ctx, as_json: bool):
    """List available pre-deploy Postgres dumps."""
    from toolkit.core.ops.db_safety import list_dumps

    root, cfg = load_root_config(ctx)
    dumps = list_dumps(cfg, root)
    if as_json:
        import json

        click.echo(
            json.dumps(
                [
                    {
                        "dump_id": record.dump_id,
                        "name": record.name,
                        "sha256": record.sha256,
                        "size_bytes": record.size_bytes,
                    }
                    for record in dumps
                ]
            )
        )
        return
    if not dumps:
        click.echo("No pre-deploy dumps found.")
        return
    click.echo(f"=== {len(dumps)} pre-deploy dump(s) ===")
    for d in dumps:
        click.echo(f"  {d.dump_id:<25} {d.name:<35} {d.size:<10}")


@maintenance.command("restore-drill")
@click.argument("dump_id", required=False)
@click.pass_context
def restore_drill_cmd(ctx, dump_id: str | None):
    """Restore a dump in isolation and issue a verified recovery checkpoint."""
    from toolkit.core.ops.db_safety import list_dumps
    from toolkit.core.ops.restore_drill import run_restore_drill

    root, cfg = load_root_config(ctx)
    dumps = list_dumps(cfg, root)
    if not dump_id:
        if not dumps:
            click.echo("No pre-deploy dumps found.")
            return
        click.echo("Available dumps (newest first):")
        for record in dumps:
            click.echo(f"  {record.dump_id:<25} {record.name:<35} {record.size:<10}")
        click.echo("\nRun: homelab-toolkit maintenance restore-drill <dump-id>")
        return
    selected = next((item for item in dumps if item.dump_id == dump_id), None)
    if selected is None:
        raise click.ClickException("dump ID is unknown, changed, or no longer available")

    click.echo(f"Running isolated restore drill for {selected.name}...")
    result = run_restore_drill(cfg, root, selected, actor="cli")
    if not result.ok:
        raise click.ClickException(result.message)
    click.secho(result.message, fg="green")
    click.echo(f"Verified checkpoint: {result.checkpoint_id}")


@maintenance.command("sync-vault")
@click.pass_context
def sync_vault(ctx):
    """Sync service credentials into Vaultwarden (idempotent).

    Pushes the credential catalog (service URLs + admin passwords from secrets)
    into the admin user's Vaultwarden, creating logins for each service. Only
    creates entries that don't exist — existing ones are preserved. Useful when
    adding new services or rotating passwords.
    """
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext
    from toolkit.services.vaultwarden.bootstrap import sync_catalog_to_vaultwarden

    root, _ = load_root_config(ctx)
    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    logs = sync_catalog_to_vaultwarden(root, cfg, secrets)
    for line in logs:
        click.echo(line)
