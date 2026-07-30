from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path

import click

from toolkit.cli._format import echo_table
from toolkit.cli.context import load_controller_client, load_root_config
from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import ConfigApplyOperation, JobRequest, ServiceActionOperation
from toolkit.controller.read_models import ManagedServiceSettingView, ServiceSettingsUpdate
from toolkit.core.compose.docker import compose_for_category, compose_for_root, profiles_for_categories
from toolkit.core.compose.registry import enabled_categories
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.placement import manifest_node, service_node
from toolkit.core.manifest.routes import compile_routes, route_scope, service_is_enabled
from toolkit.core.manifest.schema import ServiceManifest


@click.group()
def services() -> None:
    """Service management."""
    pass


def _setting_value(setting: ManagedServiceSettingView, raw: str) -> bool | int | float | str:
    if setting.type == "boolean":
        normalized = raw.strip().lower()
        if normalized not in {"true", "false"}:
            raise click.ClickException("Boolean settings must be true or false")
        return normalized == "true"
    if setting.type == "number":
        try:
            return float(raw) if any(character in raw.lower() for character in (".", "e")) else int(raw)
        except ValueError as exc:
            raise click.ClickException("Number settings require a numeric value") from exc
    if setting.type == "select" and raw not in setting.choices:
        raise click.ClickException(f"Value must be one of: {', '.join(setting.choices)}")
    return raw


@services.command("inspect")
@click.argument("name")
@click.pass_context
def inspect_service(ctx: click.Context, name: str) -> None:
    """Show one plugin's settings, actions, metrics, and resources."""
    try:
        view = load_controller_client(ctx).service_management(name)
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{view.label} ({view.service})")
    click.echo(f"State: {'enabled' if view.enabled else 'disabled'} | Node: {view.node} | Category: {view.category}")
    if view.description:
        click.echo(view.description)
    if view.settings:
        echo_table(
            [
                (
                    setting.key,
                    setting.label,
                    str(setting.value if setting.value is not None else setting.default),
                    setting.type,
                )
                for setting in view.settings
            ],
            columns=("Setting", "Label", "Value", "Type"),
        )
    if view.actions:
        echo_table(
            [(action.id, action.label, "ready" if action.can_run else "unavailable") for action in view.actions],
            columns=("Action", "Label", "State"),
        )
    if view.metrics:
        echo_table(
            [
                (metric.label, str(metric.value) if metric.value is not None else "unavailable", metric.unit or "-")
                for metric in view.metrics
            ],
            columns=("Metric", "Value", "Unit"),
        )
    for resource in view.resources:
        click.echo(f"\n{resource.label}")
        if resource.rows:
            echo_table(
                [[row.get(column.key, "-") for column in resource.columns] for row in resource.rows],
                columns=[column.label for column in resource.columns],
            )
        else:
            click.echo("No resources configured" if resource.available else "Resource status unavailable")


@services.command("set")
@click.argument("name")
@click.argument("setting_key")
@click.argument("value")
@click.pass_context
def set_service_setting(ctx: click.Context, name: str, setting_key: str, value: str) -> None:
    """Set one plugin-declared value and queue its reconciliation."""
    client = load_controller_client(ctx)
    try:
        view = client.service_management(name)
        setting = next((candidate for candidate in view.settings if candidate.key == setting_key), None)
        if setting is None:
            available = ", ".join(candidate.key for candidate in view.settings) or "none"
            raise click.ClickException(f"Unknown setting {setting_key!r}. Available: {available}")
        parsed = _setting_value(setting, value)
        updated = client.update_service_settings(
            name,
            ServiceSettingsUpdate(expected_revision=view.revision, values={setting_key: parsed}),
        )
        click.echo(f"Saved {setting_key}={str(parsed).lower() if isinstance(parsed, bool) else parsed}")
        if setting.requires_redeploy:
            job = client.submit(
                JobRequest(
                    idempotency_key=str(uuid.uuid4()),
                    operation=ConfigApplyOperation(revision_hash=updated.revision, service=name),
                )
            )
            click.echo(f"Reconciliation queued as job {job.job_id}")
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc


@services.command("run")
@click.argument("name")
@click.argument("action_id")
@click.option("--yes", is_flag=True, help="Confirm the declared operation without prompting")
@click.pass_context
def run_service_action(ctx: click.Context, name: str, action_id: str, yes: bool) -> None:
    """Queue one action declared and implemented by a service plugin."""
    client = load_controller_client(ctx)
    try:
        view = client.service_management(name)
        action = next((candidate for candidate in view.actions if candidate.id == action_id), None)
        if action is None:
            available = ", ".join(candidate.id for candidate in view.actions) or "none"
            raise click.ClickException(f"Unknown action {action_id!r}. Available: {available}")
        if not action.can_run:
            raise click.ClickException(f"Action {action_id!r} is not available while the service is disabled")
        if action.confirmation and not yes:
            click.confirm(action.confirmation, abort=True)
        job = client.submit(
            JobRequest(
                idempotency_key=str(uuid.uuid4()),
                operation=ServiceActionOperation(service=name, action=action_id),
            )
        )
        click.echo(f"{action.label} queued as job {job.job_id}")
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc


@services.command("list")
@click.pass_context
def list_services(ctx: click.Context):
    """List all enabled services."""
    _root, cfg = load_root_config(ctx)
    catalog = load_service_catalog()
    grouped: dict[tuple[str, str], list[ServiceManifest]] = defaultdict(list)
    for manifest in catalog.manifests:
        if service_is_enabled(cfg, manifest):
            grouped[(manifest.category, manifest_node(cfg, manifest))].append(manifest)

    total = 0
    for (category, vm), manifests in grouped.items():
        click.echo(f"\n{category.replace('-', ' ').title()} ({vm}): {len(manifests)} services")
        for manifest in manifests:
            click.echo(f"  {manifest.name:28s} {manifest.description}")
        total += len(manifests)
    click.echo(f"\nTotal: {total} services")


@services.command()
@click.argument("name", required=False)
@click.pass_context
def routes(ctx: click.Context, name: str | None):
    """Show service routes/subdomains."""
    _root, cfg = load_root_config(ctx)
    selected = [route for route in compile_routes(cfg) if not name or route.service == name]
    click.echo(f"{'Service':<24} {'Exposure':<9} {'Auth':<14} {'Scope':<42} URL")
    click.echo("-" * 120)
    for route in selected:
        click.echo(
            f"{route.service:<24} {route.exposure:<9} {route.auth.mode:<14} "
            f"{route_scope(route):<42} https://{route.host}"
        )


@services.command("status")
@click.pass_context
def service_status(ctx):
    """Show live service health and container status."""
    try:
        inventory = load_controller_client(ctx).container_inventory()
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc

    by_node = defaultdict(list)
    for container in inventory.containers:
        by_node[container.node].append(container)
    for node in sorted(by_node):
        click.echo(f"\n[{node}]")
        click.echo(f"{'Service':<25} {'State':<12} {'Health':<12} {'Image'}")
        click.echo("-" * 70)
        for container in sorted(by_node[node], key=lambda item: item.name):
            click.echo(f"{container.name:<25} {container.state:<12} {container.health:<12} {container.image}")
    for node in inventory.unavailable_nodes:
        click.echo(f"[{node}] Container inventory unavailable")
    if not inventory.is_available:
        ctx.exit(1)


@services.command("start")
@click.argument("name", required=False)
@click.option("--category", "-c", default=None)
@click.pass_context
def start_services(ctx, name, category):
    """Start services (all, by category, or by name).

    With a NAME, starts only that service on its owning VM (resolved via the
    category registry). Without a NAME, starts the whole stack per VM.
    """
    root, cfg = load_root_config(ctx)
    cats = enabled_categories(cfg)
    if category:
        cats = [c for c in cats if c.name == category]

    if name:
        # Resolve name → owning category (mirror restart's pattern) so we start
        # only the named service on the one VM that has it — not fan out to every VM.
        for cat in cats:
            if name not in {s.name for s in cat.services(cfg)}:
                continue
            node = service_node(cfg, name)
            dc = compose_for_root(cfg, root, vm=node if cfg.is_multi_node else None)
            if dc is None:
                raise click.ClickException(f"Compose model missing for {node}")
            click.echo(f"Starting {name} on {node}...")
            dc.up(services=[name])
            click.echo("Done.")
            return
        click.echo(f"Service '{name}' not found. Run 'services list' to see names.")
        return

    by_vm: dict[str, list] = defaultdict(list)
    for cat in cats:
        vm = cat.runtime_node(cfg)
        by_vm[vm].append(cat)

    for vm, vm_cats in sorted(by_vm.items()):
        dc = compose_for_root(cfg, root, vm=vm if cfg.is_multi_node else None)
        if not dc:
            click.echo(f"Skip {vm}: no docker-compose.yml")
            continue
        profs = profiles_for_categories(vm_cats)
        click.echo(f"Starting on {vm} (profiles: {', '.join(profs)})...")
        dc.up(profiles=profs)
    click.echo("Done.")


@services.command("stop")
@click.argument("name", required=False)
@click.option("--category", "-c", default=None)
@click.pass_context
def stop_services(ctx, name, category):
    """Stop services (all, by category, or by name).

    With a NAME, stops only that service on its owning VM — NOT the whole
    stack. (The previous implementation ignored NAME and called dc.down() with
    no filter, stopping every service on the VM.)
    """
    root, cfg = load_root_config(ctx)
    cats = enabled_categories(cfg)
    if category:
        cats = [c for c in cats if c.name == category]

    if name:
        # Resolve name → owning category (mirror restart's pattern) so we stop
        # only the named service — not the whole stack.
        for cat in cats:
            if name not in {s.name for s in cat.services(cfg)}:
                continue
            node = service_node(cfg, name)
            dc = compose_for_root(cfg, root, vm=node if cfg.is_multi_node else None)
            if dc is None:
                raise click.ClickException(f"Compose model missing for {node}")
            click.echo(f"Stopping {name} on {node}...")
            dc.down(services=[name])
            click.echo("Done.")
            return
        click.echo(f"Service '{name}' not found. Run 'services list' to see names.")
        return

    stopped_vms: set[str] = set()
    for cat in cats:
        vm = cat.runtime_node(cfg)
        if vm in stopped_vms:
            continue
        stopped_vms.add(vm)
        dc = compose_for_root(cfg, root, vm=vm if cfg.is_multi_node else None)
        if not dc:
            continue
        click.echo(f"Stopping stack on {vm}...")
        dc.down()
    click.echo("Done.")


@services.command("restart")
@click.argument("name", required=False)
@click.option("--category", "-c", default=None)
@click.pass_context
def restart_services(ctx, name, category):
    """Restart services (all, by category, or by name)."""
    root, cfg = load_root_config(ctx)
    cats = enabled_categories(cfg)
    if category:
        cats = [c for c in cats if c.name == category]

    if name:
        for cat in cats:
            if name not in {s.name for s in cat.services(cfg)}:
                continue
            node = service_node(cfg, name)
            dc = compose_for_root(cfg, root, vm=node if cfg.is_multi_node else None)
            if dc is None:
                raise click.ClickException(f"Compose model missing for {node}")
            click.echo(f"Restarting {name} on {node}...")
            dc.restart(services=[name])
            click.echo("Done.")
            return
        click.echo(f"Service '{name}' not found.")
        return

    by_vm: dict[str, list] = defaultdict(list)
    for cat in cats:
        vm = cat.runtime_node(cfg)
        by_vm[vm].append(cat)

    for vm in sorted(by_vm.keys()):
        dc = compose_for_root(cfg, root, vm=vm if cfg.is_multi_node else None)
        if not dc:
            continue
        click.echo(f"Restarting stack on {vm}...")
        dc.restart()
    click.echo("Done.")


@services.command("logs")
@click.argument("name")
@click.option("--tail", "-n", default=100, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.pass_context
def service_logs(ctx, name, tail, follow):
    """View logs for a service."""
    root, cfg = load_root_config(ctx)

    for cat in enabled_categories(cfg):
        svc_names = [s.name for s in cat.services(cfg)]
        if name not in svc_names:
            continue
        dc = compose_for_category(cat, cfg, root)
        output = dc.logs(services=[name], tail=tail, follow=follow)
        if output:
            click.echo(output)
        return

    click.echo(f"Service '{name}' not found.")


@services.command("enable")
@click.argument("name")
@click.option("--disable", is_flag=True, help="Disable instead of enable")
@click.pass_context
def enable_service(ctx, name, disable):
    """Enable or disable a service category."""
    root = Path(ctx.obj["root"])
    from toolkit.cli.config_mutation import cli_configuration_mutation
    from toolkit.core.compose.registry import all_categories, load_all
    from toolkit.core.config.config import Config, ServicesConfig, load_config, save_config
    from toolkit.core.config.storage import config_path

    cp = config_path(root)
    if not cp.exists():
        raise click.ClickException("No config.yaml. Run: homelab-toolkit config init")

    load_all()
    categories = {category.name: category for category in all_categories()}
    category = categories.get(name)
    if category is None:
        available = ", ".join(sorted(categories))
        raise click.ClickException(f"Unknown service category '{name}'. Available categories: {available}")

    with cli_configuration_mutation(root, "service-toggle"):
        cfg = load_config(cp)
        if disable and category.always_on:
            raise click.ClickException(f"Service category '{name}' is always-on and cannot be disabled")
        if disable:
            dependents = sorted(
                candidate.name
                for candidate in categories.values()
                if name in candidate.depends_on() and cfg.category_enabled(candidate.name)
            )
            if dependents:
                raise click.ClickException(
                    f"Service category '{name}' is required by enabled categories: {', '.join(dependents)}"
                )

        service_values = cfg.services.model_dump(mode="python")
        service_values[name] = not disable
        services = ServicesConfig.model_validate(service_values)
        updated = Config.model_validate(
            {**cfg.model_dump(mode="python"), "services": services.model_dump(mode="python")}
        )
        save_config(updated, cp)

    action = "Disabled" if disable else "Enabled"
    click.echo(f"{action} '{name}'. Run 'generate' to apply.")


@services.command("deploy")
@click.argument("name")
@click.pass_context
def deploy_service(ctx, name):
    """Queue exact service reconciliation through the durable controller."""
    client = load_controller_client(ctx)
    try:
        view = client.service_management(name)
        if not view.enabled:
            raise click.ClickException(f"Service {name!r} is disabled")
        job = client.submit(
            JobRequest(
                idempotency_key=str(uuid.uuid4()),
                operation=ConfigApplyOperation(revision_hash=view.revision, service=name),
            )
        )
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{view.label} reconciliation queued on {view.node} as job {job.job_id}")
