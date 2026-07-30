from __future__ import annotations

from pathlib import Path

import click


@click.group()
def fleet():
    """Manage external servers and fleet nodes (Komodo, mesh, LDAP, agents)."""
    pass


def _resolve_node(root: Path, name: str):
    """Look up any managed external host."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    cfg = load_config(config_path(root))
    return next((host for host in cfg.external_hosts if host.name == name), None)


@fleet.command("list")
@click.pass_context
def list_nodes(ctx):
    """List configured fleet nodes and external hosts."""
    root = Path(ctx.obj["root"])
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    cfg = load_config(config_path(root))
    if not cfg.external_hosts:
        click.echo("No managed hosts configured.")
        return

    click.echo(f"{'Name':<18} {'IP':<18} {'Kind':<8} {'Reconciled':<11} {'Group':<14} {'Services'}")
    click.echo("-" * 116)
    for host in cfg.external_hosts:
        click.echo(
            f"{host.name:<18} {host.ip:<18} {host.kind:<8} "
            f"{'yes' if host.reconciled else 'no':<11} "
            f"{host.cluster_group or '-':<14} "
            f"{', '.join(host.services) or '(none)'}"
        )


@fleet.command("add")
@click.argument("name", required=False)
@click.argument("ip", required=False)
@click.option("--user", "-u", default="root", help="SSH user")
@click.option("--port", "-p", default=22, type=int, help="SSH port")
@click.option("--cluster-group", default="", help="Komodo server group to join after onboarding")
@click.option("--lldap-email", default="", help="Admin LLDAP/SSO account email for this node")
@click.option(
    "--headscale-tag",
    "headscale_tags",
    multiple=True,
    help="Headscale ACL tags for mesh join (default: config fleet.headscale_tags)",
)
@click.option(
    "--service",
    "-s",
    "services",
    multiple=True,
    help="Select a manifest-declared host integration (repeatable). Defaults to the host-type baseline.",
)
@click.option(
    "--integration-setting",
    "integration_settings",
    multiple=True,
    metavar="SERVICE.FIELD=VALUE",
    help="Set a service-owned host integration field (repeatable).",
)
@click.option(
    "--plain",
    is_flag=True,
    help="Register as a plain external host (no Komodo Periphery, mesh, or LDAP onboarding)",
)
@click.option(
    "--skip-onboard",
    is_flag=True,
    help="Register only; do not run fleet onboard playbook",
)
@click.pass_context
def add_node(
    ctx,
    name,
    ip,
    user,
    port,
    cluster_group,
    lldap_email,
    headscale_tags,
    services,
    integration_settings,
    plain,
    skip_onboard,
):
    """Add an external server and (by default) run full fleet onboarding.

    By default the host is registered as a fleet node and onboarded with the
    baseline agents (Komodo Periphery, Wazuh, monitoring, Headscale mesh, DNS,
    LDAP SSH). Use --service to select integrations or accept manifest defaults.
    Use --plain for a NAS/cache server that only
    needs agent deployment (no Komodo/mesh/LDAP).
    """
    root = Path(ctx.obj["root"])
    from toolkit.core.config.config import config_path, load_config
    from toolkit.core.config.validators import IPV4_REGEX_STRICT
    from toolkit.core.infra.fleet import add_node as _add_node
    from toolkit.core.infra.fleet import onboard_node as _onboard_node
    from toolkit.core.infra.fleet_roles import (
        FLEET_SELECTABLE_SERVICES,
        fleet_default_services,
        parse_host_integration_assignments,
        plain_host_default_services,
    )

    cfg = load_config(config_path(root))
    if not name:
        name = click.prompt("Node name (inventory hostname)", type=str).strip()
    if not ip:
        ip = click.prompt("Public IPv4", type=str).strip()
    if not IPV4_REGEX_STRICT.match(ip):
        raise click.BadParameter(f"Invalid IPv4 address: '{ip}'")
    defaults = plain_host_default_services() if plain else fleet_default_services()
    selected_services = list(services) if services else defaults
    if selected_services:
        unknown = [s for s in selected_services if s not in FLEET_SELECTABLE_SERVICES]
        if unknown:
            raise click.BadParameter(
                f"Unknown service(s): {', '.join(unknown)}. Valid: {', '.join(FLEET_SELECTABLE_SERVICES)}"
            )
    try:
        integrations = parse_host_integration_assignments(selected_services, integration_settings)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if plain:
        # Plain external host (NAS, cache server) — no fleet onboarding metadata.
        from toolkit.core.infra.hosts import add_host as _add_host
        from toolkit.core.infra.hosts import reconcile_host_integrations
        from toolkit.core.ops.dns import external_host_fqdn

        selected = selected_services
        # Plain hosts can't take fleet-only services.
        if "ldap-client" in selected:
            selected = [s for s in selected if s != "ldap-client"]
        host = _add_host(
            root,
            name,
            ip,
            ssh_user=user,
            ssh_port=port,
            services=selected,
            integrations=integrations,
        )
        click.echo(f"Added host '{name}' ({ip})")
        fqdn = external_host_fqdn(name, load_config(config_path(root)).domain)
        click.echo(f"  DNS: {fqdn} → {ip} (Cloudflare if token set; AdGuard on deploy hooks)")
        integration_result = reconcile_host_integrations(root, host)
        for message in integration_result.logs:
            click.echo(f"  {message}")
        if not integration_result.ok:
            click.echo(f"  Reconciliation pending: {len(integration_result.errors)} integration error(s)")
        elif integration_result.refresh_nodes:
            import asyncio

            from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

            click.echo(f"  Refreshing runtime nodes: {', '.join(integration_result.refresh_nodes)}")
            refresh = asyncio.run(
                run_deploy_workflow(
                    root,
                    load_config(config_path(root)),
                    on_log=lambda line: click.echo(f"    {line}"),
                    on_step=lambda step, state: click.echo(f"    {step}: {state}"),
                    skip_infra=True,
                    skip_dns=True,
                    targets=integration_result.refresh_nodes,
                )
            )
            if not refresh.success:
                raise click.ClickException("managed-host runtime refresh failed")
        click.echo(f"  Services: {', '.join(selected)}")
        click.echo(f"\nTest connection: homelab-toolkit fleet test {name}")
        click.echo(f"Deploy agents:   homelab-toolkit fleet deploy {name}")
        return

    if not lldap_email:
        lldap_email = cfg.email or ""
    if not cluster_group:
        cluster_group = ""

    try:
        tags = list(headscale_tags) if headscale_tags else list(cfg.fleet.headscale_tags)
        node = _add_node(
            root,
            name,
            ip,
            ssh_user=user,
            ssh_port=port,
            cluster_group=cluster_group,
            lldap_email=lldap_email,
            headscale_tags=tags,
            services=selected_services,
            integrations=integrations,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Added fleet node '{node.name}' ({node.ip}) — mesh tags: {', '.join(tags)}")
    click.echo(f"  Services: {', '.join(node.services)}")
    if node.cluster_group:
        click.echo(f"  Cluster group: {node.cluster_group}")
    if skip_onboard:
        click.echo(f"\nOnboard later: homelab-toolkit fleet onboard {name}")
        return

    click.echo(f"\nOnboarding {name} (mesh, Komodo, LDAP, agents)...")
    result = _onboard_node(root, name, on_log=click.echo)
    if not result.success:
        raise click.ClickException(result.message)
    click.echo(result.message)


@fleet.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def remove_node(ctx, name, yes):
    """Remove a fleet node or external host."""
    root = Path(ctx.obj["root"])
    from toolkit.core.infra.fleet import get_node
    from toolkit.core.infra.fleet import remove_node as _remove_node

    if not yes:
        click.confirm(f"Remove '{name}' and clean up its managed integrations?", abort=True)

    node = get_node(root, name)
    if node is not None:
        if _remove_node(root, name):
            click.echo(f"Removed fleet node '{name}'.")
        else:
            click.echo(f"Fleet node '{name}' not found.")
        return

    # Plain external host.
    from toolkit.core.infra.hosts import remove_host as _remove_host

    if _remove_host(root, name):
        click.echo(f"Removed host '{name}'.")
    else:
        click.echo(f"Host '{name}' not found.")


@fleet.command("onboard")
@click.argument("name")
@click.pass_context
def onboard_node(ctx, name):
    """Run manifest-configured onboarding for one fleet node."""
    root = Path(ctx.obj["root"])
    from toolkit.core.infra.fleet import onboard_node as _onboard_node

    result = _onboard_node(root, name, on_log=click.echo)
    if not result.success:
        raise click.ClickException(result.message)
    click.echo(result.message)


@fleet.command("status")
@click.argument("name", required=False)
@click.pass_context
def fleet_status(ctx, name):
    """Show SSH and agent status for one or all fleet nodes."""
    root = Path(ctx.obj["root"])
    from toolkit.core.infra.fleet import all_node_statuses, node_status

    if name:
        status = node_status(root, name)
        if not status:
            click.echo(f"Fleet node '{name}' not found.")
            ctx.exit(1)
        _print_status(status)
        return

    statuses = all_node_statuses(root)
    if not statuses:
        click.echo("No fleet nodes configured.")
        return

    for status in statuses:
        _print_status(status)
        click.echo("")


@fleet.command("test")
@click.argument("name")
@click.pass_context
def test_node(ctx, name):
    """Test SSH connectivity to a fleet node or external host."""
    root = Path(ctx.obj["root"])
    node = _resolve_node(root, name)
    if node is None:
        click.echo(f"'{name}' not found.")
        ctx.exit(1)
        return

    from toolkit.core.infra.hosts import test_host_connection

    click.echo(f"Testing connection to {node.ip}...")
    ok = test_host_connection(node, root=root)
    if ok:
        click.echo(f"  Connected to {node.ip}")
    else:
        click.echo(f"  Failed to connect to {node.ip}")
        ctx.exit(1)


@fleet.command("trust")
@click.argument("name")
@click.pass_context
def trust_node(ctx, name):
    """Add a node's SSH key to known_hosts for StrictHostKeyChecking."""
    root = Path(ctx.obj["root"])
    node = _resolve_node(root, name)
    if node is None:
        click.echo(f"'{name}' not found.")
        ctx.exit(1)
        return

    from toolkit.core.infra.hosts import trust_host_key

    for line in trust_host_key(node, root=root):
        click.echo(line)


@fleet.command("deploy")
@click.argument("name")
@click.pass_context
def deploy_node(ctx, name):
    """Deploy configured services to an external host via Ansible."""
    root = Path(ctx.obj["root"])
    node = _resolve_node(root, name)
    if node is None:
        click.echo(f"'{name}' not found.")
        ctx.exit(1)
        return

    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.deploy.external_deploy import deploy_external_host

    cfg = load_config(config_path(root))
    result = deploy_external_host(root, cfg, node, on_log=click.echo)
    if not result.success:
        ctx.exit(1)
    click.echo(result.message)


def _print_status(status) -> None:
    def _fmt(value: bool | None) -> str:
        if value is None:
            return "unknown"
        return "ok" if value else "fail"

    click.echo(f"{status.name}:")
    click.echo(f"  SSH:        {_fmt(status.ssh_ok)}")
    for agent in status.agents:
        click.echo(f"  {agent.label}: {_fmt(agent.active)} ({agent.detail})")
    click.echo(f"  Reconciled: {'yes' if status.reconciled else 'no'}")
