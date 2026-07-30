"""Local operator commands for first-run controller bootstrap."""

from __future__ import annotations

import click

from toolkit.controller.client import ControllerClientError


@click.group("bootstrap")
def bootstrap() -> None:
    """Authorize the secure first-run setup flow."""


@bootstrap.command("token")
@click.pass_context
def bootstrap_token(ctx: click.Context) -> None:
    """Issue a short-lived, one-time capability for the setup page."""
    from toolkit.cli.context import load_controller_client

    controller = load_controller_client(ctx)
    try:
        capability = controller.issue_bootstrap_capability()
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Bootstrap setup capability (expires {capability.expires_at.isoformat()}):")
    click.echo(capability.token)
    click.echo()
    click.echo("Open /setup on the Homelab UI and paste this capability when prompted.")
