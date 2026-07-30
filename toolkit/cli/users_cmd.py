"""Manage LLDAP directory users (central homelab accounts)."""

from __future__ import annotations

import getpass

import click
import httpx

from toolkit.cli._format import echo_table
from toolkit.core.config.storage import secrets_path
from toolkit.core.identity.lldap_client import LLDAPClient, user_id_from_email
from toolkit.core.identity.service_groups import HOMELAB_GROUP_NAMES
from toolkit.core.secrets.secrets import load_secrets_plaintext


class _DirectoryCommandGroup(click.Group):
    """Render directory transport failures as actionable CLI errors."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except httpx.HTTPError as exc:
            raise click.ClickException(
                "The private LLDAP API is unreachable from this host. "
                "Use the Homelab UI People page, or run this command on the managed control node."
            ) from exc


@click.group(cls=_DirectoryCommandGroup)
def users():
    """LLDAP user directory (Authelia reads this — single account source)."""
    pass


def _client(ctx: click.Context) -> LLDAPClient:
    from toolkit.cli import load_root_config

    root, _cfg = load_root_config(ctx)
    secrets = load_secrets_plaintext(secrets_path(root))
    admin = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin:
        raise click.ClickException("LLDAP_ADMIN_PASSWORD missing in secrets.enc.yaml")
    return LLDAPClient(admin_password=admin, root=root)


@users.command("list")
@click.pass_context
def users_list(ctx: click.Context):
    """List LLDAP users."""
    from toolkit.cli.context import load_controller_client
    from toolkit.controller.client import ControllerClientError

    try:
        directory = load_controller_client(ctx).directory_users()
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = [(user.id, user.email, user.display_name or "-") for user in directory.users]
    echo_table(rows, columns=("User", "Email", "Display name"))


@users.command("invite")
@click.argument("email")
@click.option("--display-name", default="", help="Display name")
@click.option(
    "--groups",
    multiple=True,
    type=click.Choice(HOMELAB_GROUP_NAMES),
    help="Plugin-defined homelab access groups",
)
@click.option("--no-notify", is_flag=True, help="Skip owner ntfy notification")
@click.pass_context
def users_invite(ctx: click.Context, email: str, display_name: str, groups: tuple[str, ...], no_notify: bool):
    """Invite a user — they set their own password via Authelia reset."""
    from toolkit.cli import load_root_config
    from toolkit.core.identity.service_groups import default_user_groups_for_enabled_services
    from toolkit.core.identity.user_provision import invite_and_provision_user

    root, cfg = load_root_config(ctx)
    secrets = load_secrets_plaintext(secrets_path(root))
    client = _client(ctx)
    selected = list(groups) if groups else default_user_groups_for_enabled_services(cfg.services)
    for line in invite_and_provision_user(
        cfg,
        secrets,
        client,
        email,
        display_name=display_name or None,
        groups=selected,
        notify=not no_notify,
        root=root,
    ):
        click.echo(line)


@users.command("create")
@click.argument("email")
@click.option("--display-name", default="", help="Display name")
@click.option(
    "--groups",
    multiple=True,
    type=click.Choice(HOMELAB_GROUP_NAMES),
    help="Plugin-defined homelab access groups",
)
@click.pass_context
def users_create(ctx: click.Context, email: str, display_name: str, groups: tuple[str, ...]):
    """Create a user (default: invite flow with self-service password)."""
    from toolkit.cli import load_root_config
    from toolkit.core.identity.service_groups import default_user_groups_for_enabled_services
    from toolkit.core.identity.user_provision import invite_and_provision_user

    root, cfg = load_root_config(ctx)
    secrets = load_secrets_plaintext(secrets_path(root))
    client = _client(ctx)
    pw = None
    if click.get_text_stream("stdin").isatty():
        pw = getpass.getpass("Password (leave empty for invite flow): ") or None

    selected = list(groups) if groups else default_user_groups_for_enabled_services(cfg.services)
    for line in invite_and_provision_user(
        cfg,
        secrets,
        client,
        email,
        display_name=display_name or None,
        groups=selected,
        password=pw,
        root=root,
    ):
        click.echo(line)


@users.command("provision")
@click.argument("email")
@click.option("--no-notify", is_flag=True, help="Skip ntfy invite notification")
@click.pass_context
def users_provision(ctx: click.Context, email: str, no_notify: bool):
    """Re-send service invites for an existing LLDAP user."""
    from toolkit.cli import load_root_config
    from toolkit.core.identity.user_provision import provision_user_services

    root, cfg = load_root_config(ctx)
    client = _client(ctx)
    user = client.find_user(email)
    if not user:
        raise click.ClickException(f"No LLDAP user for {email}")
    groups = client.user_group_names(user.id)
    secrets = load_secrets_plaintext(secrets_path(root))
    report = provision_user_services(cfg, secrets, user.email, groups, notify=not no_notify, root=root)
    for line in report.messages:
        click.echo(line)


@users.command("provision-all")
@click.pass_context
def users_provision_all(ctx: click.Context):
    """Re-provision all directory users into their services."""
    from toolkit.cli import load_root_config
    from toolkit.core.identity.user_provision import provision_all_directory_users

    root, cfg = load_root_config(ctx)
    secrets = load_secrets_plaintext(secrets_path(root))
    for line in provision_all_directory_users(cfg, secrets, root=root):
        click.echo(line)


@users.command("set-password")
@click.argument("email")
@click.pass_context
def users_set_password(ctx: click.Context, email: str):
    """Update a user's password (admin override)."""
    client = _client(ctx)
    user_id = user_id_from_email(email)
    for user in client.list_users():
        if user.email.lower() == email.lower():
            user_id = user.id
            break
    pw = getpass.getpass("New password: ")
    client.set_password(user_id, pw)
    click.echo(f"Password updated for {user_id}")


@users.command("delete")
@click.argument("user")
@click.option("--yes", "-y", is_flag=True, help="Delete without prompting")
@click.pass_context
def users_delete(ctx: click.Context, user: str, yes: bool):
    """Delete a user by email or id."""
    client = _client(ctx)
    target = None
    lookup = user.strip().lower()
    for item in client.list_users():
        if item.email.lower() == lookup or item.id.lower() == lookup:
            target = item
            break
    if target is None:
        raise click.ClickException(f"LLDAP user not found: {user}")
    click.echo(f"User: {target.id} <{target.email}>")
    if not yes and not click.confirm("Delete this LLDAP user?", default=False):
        click.echo("Aborted.")
        return
    client.delete_user(target.id)
    click.echo(f"Deleted {target.id}")
