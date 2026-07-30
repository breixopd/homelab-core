from __future__ import annotations

from pathlib import Path

import click

from toolkit.core.config.config import load_config
from toolkit.core.config.storage import config_path
from toolkit.core.generate.generate import generate_all, generate_configs


@click.command()
@click.option("--skip-validate", is_flag=True, help="Skip post-generation artifact validation")
@click.option("--inventory-only", is_flag=True, help="Only write Ansible inventory (skip .env and config generation)")
@click.option("--proxmox-only", is_flag=True, help="With --inventory-only: only proxmox_hosts (for host-setup)")
@click.option("--from-tofu", is_flag=True, help="With --inventory-only: merge OpenTofu machine_ips output")
@click.pass_context
def generate(ctx: click.Context, skip_validate: bool, inventory_only: bool, proxmox_only: bool, from_tofu: bool):
    """Generate .env and config files for all nodes."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))

    if inventory_only:
        from toolkit.core.ansible.ansible_inventory import (
            ensure_group_vars_all,
            parse_tofu_machine_ips,
            write_inventory,
        )

        ensure_group_vars_all(root)
        ips = parse_tofu_machine_ips(root / "infrastructure") if from_tofu else None
        path = write_inventory(root, cfg, machine_ips=ips, proxmox_only=proxmox_only)
        click.echo(f"Wrote {path}")
        return

    click.echo("Generating runtime environment and Compose models...")
    results = generate_all(root)
    if (root / "docker-compose.yml").is_file():
        click.echo("  compose: docker-compose.yml")
        for role in cfg.enabled_nodes if cfg.is_multi_node else ():
            path = root / "generated" / role / "compose.yaml"
            if path.is_file():
                click.echo(f"  compose[{role}]: {path.relative_to(root)}")
    for vm, path in results.items():
        click.echo(f"  {vm}: {path}")

    click.echo("Generating service-owned artifacts...")

    def artifact_progress(completed: int, total: int, service: str) -> None:
        click.echo(f"  artifact [{completed}/{total}]: {service}")

    configs = generate_configs(cfg, root, on_progress=artifact_progress)
    for p in configs:
        click.echo(f"  config: {p}")

    infra_dir = root / "infrastructure"
    if infra_dir.is_dir():
        from toolkit.core.ansible.ansible_inventory import (
            ensure_group_vars_all,
            parse_tofu_machine_ips,
            write_inventory,
        )
        from toolkit.core.infra.iac_sync import sync_from_repo_root

        click.echo("Synchronizing infrastructure and inventory...")
        tf, ans = sync_from_repo_root(root)
        click.echo(f"  sync: {tf.relative_to(root)}, {ans.relative_to(root)}")
        ensure_group_vars_all(root)
        ips = parse_tofu_machine_ips(infra_dir)
        inv = write_inventory(root, cfg, machine_ips=ips or None)
        click.echo(f"  inventory: {inv.relative_to(root)}")

    if not skip_validate:
        from toolkit.core.generate.validate import validate_generated_artifacts

        click.echo("Validating generated deployment artifacts...")
        report = validate_generated_artifacts(root)
        for check in report.checks:
            click.echo(f"  validate: OK - {check}")
        for skipped in report.skipped:
            click.echo(f"  validate: SKIP - {skipped}")
        for warning in report.warnings:
            click.echo(f"  validate: WARN - {warning}")
        for error in report.errors:
            click.echo(f"  validate: ERROR - {error}")
        if report.errors:
            raise SystemExit(1)

    from toolkit.core.registry.reconcile import write_last_reconcile

    reconcile_path = write_last_reconcile(root, cfg, trigger="generate")
    click.echo(f"  reconcile: {reconcile_path.relative_to(root)}")

    click.echo(f"\nGenerated config for {len(results)} node(s), {len(configs)} config file(s)")
