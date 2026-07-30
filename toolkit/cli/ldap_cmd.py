from __future__ import annotations

from pathlib import Path

import click

from toolkit.cli import load_root_config


@click.group(hidden=True)
def ldap():
    """SSSD sync (automated on deploy and fleet onboard — manual use for recovery)."""
    pass


@ldap.command("sync")
@click.option("--repair", is_flag=True, help="Repair LLDAP POSIX attrs before SSSD sync")
@click.option("--limit", default=None, help="Ansible limit (infra, media, apps, fleet node)")
@click.pass_context
def ldap_sync(ctx: click.Context, repair: bool, limit: str | None):
    """Push ldap-client to guests/fleet (normally automatic after deploy)."""
    root, _cfg = load_root_config(ctx)
    from toolkit.core.identity.ldap_automation import ensure_directory_and_sssd

    for line in ensure_directory_and_sssd(Path(root), limit=limit, repair=repair):
        click.echo(line)
    click.echo("LDAP sync complete.")
