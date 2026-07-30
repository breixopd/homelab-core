from __future__ import annotations

import click

from toolkit.cli import load_root_config
from toolkit.core.config.config import ToolkitState, get_state
from toolkit.core.ops.manual_steps import format_manual_steps_cli, get_manual_steps


@click.group(invoke_without_command=True)
@click.pass_context
def ops(ctx: click.Context):
    """Homelab status and remaining human steps (automation handles the rest)."""
    if ctx.invoked_subcommand is None:
        _show_ops(ctx)


def _show_ops(ctx: click.Context) -> None:
    root, cfg = load_root_config(ctx)
    state = get_state(root)
    click.echo(f"State:  {state.value}")
    click.echo(f"Domain: {cfg.domain}")
    click.echo(f"Nodes:  {', '.join(cfg.enabled_nodes) or 'single-host'}")

    required = [s for s in get_manual_steps(cfg) if s.category == "Required"]
    if required:
        click.echo("")
        click.echo(format_manual_steps_cli(required))
    elif state == ToolkitState.UNINITIALIZED:
        click.echo("\nRun: homelab-toolkit install")
    else:
        click.echo("\nAll automated — deploy and verify run hooks, DNS, LDAP, and mesh for you.")
        click.echo("  homelab-toolkit deploy all")
        click.echo("  homelab-toolkit deploy verify --hooks")
