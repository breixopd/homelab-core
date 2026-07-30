from __future__ import annotations

from pathlib import Path

import click
from pydantic import ValidationError

from toolkit.core.config.config import Config, ProjectEntry, load_config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.generate.generate import run_full_generate


@click.group()
def projects():
    """Manage custom subdomain projects (Caddy routes, DNS, SSL)."""
    pass


@projects.command("list")
@click.pass_context
def list_projects(ctx):
    """List all registered projects."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    entries = cfg.projects.entries if cfg.projects else []

    if not entries:
        click.echo("No projects registered.")
        return

    from toolkit.core.projects.placement import project_node

    click.echo(
        f"{'Subdomain':<25} {'Name':<20} {'Placement':<14} {'Node':<12} "
        f"{'Database':<16} {'Exposure':<10} {'Auth':<14} {'Portal'}"
    )
    click.echo("-" * 138)
    for p in entries:
        portal_status = "yes" if p.show_on_portal else "no"
        fqdn = f"{p.subdomain}.{cfg.domain}"
        click.echo(
            f"{fqdn:<25} {p.name or p.subdomain:<20} {p.placement:<14} {project_node(cfg, p):<12} "
            f"{p.database_service or '-':<16} {p.exposure:<10} {p.auth_mode:<14} {portal_status}"
        )


@projects.command("add")
@click.option("--subdomain", "-s", required=True, help="Subdomain (e.g. 'blog' → blog.DOMAIN)")
@click.option("--name", "-n", default="", help="Display name for the portal")
@click.option("--description", "-d", default="", help="Short description")
@click.option(
    "--auth-mode",
    type=click.Choice(["forward_auth", "native"]),
    default="forward_auth",
    show_default=True,
    help="Authentication enforced at ingress or by the application",
)
@click.option(
    "--exposure",
    type=click.Choice(["private", "public"]),
    default="private",
    show_default=True,
    help="Private mesh/LAN access or public internet access",
)
@click.option("--portal/--no-portal", "show_on_portal", default=True, help="Show on root landing page (default: on)")
@click.option("--image", "-i", default=None, help="Immutable Docker image pinned by sha256 digest")
@click.option("--port", "-p", type=int, default=None, help="Container port (default 80)")
@click.option("--placement", default=None, help="Machine ID or unique capability label")
@click.option("--database", "database_service", default="", help="Managed database provider service")
@click.pass_context
def add_project(
    ctx, subdomain, name, description, auth_mode, exposure, show_on_portal, image, port, placement, database_service
):
    """Register a new project with auto Caddy route + CF DNS."""
    root = Path(ctx.obj["root"])
    if not image:
        raise click.ClickException("An immutable --image is required for declarative projects.")
    from toolkit.cli.config_mutation import cli_configuration_mutation
    from toolkit.core.projects.placement import default_project_placement, project_node

    with cli_configuration_mutation(root, "project-add"):
        cfg = load_config(config_path(root))
        target = placement or default_project_placement(cfg)
        try:
            entry = ProjectEntry(
                name=name or subdomain,
                subdomain=subdomain,
                auth_mode=auth_mode,
                exposure=exposure,
                description=description,
                show_on_portal=show_on_portal,
                docker_image=image,
                container_port=port or 80,
                placement=target,
                database_service=database_service,
            )
        except (ValidationError, ValueError) as exc:
            raise click.ClickException(f"Invalid project definition: {exc}") from exc

        if any(p.subdomain == subdomain for p in cfg.projects.entries):
            raise click.ClickException(f"Project with subdomain '{subdomain}' already exists.")

        from toolkit.core.compose.port_conflict import check_container_name, check_port_conflict

        try:
            node = project_node(cfg, entry)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        conflicts = check_port_conflict(node, entry.container_port, cfg)
        if conflicts:
            raise click.ClickException(f"Port {entry.container_port} conflicts with: {', '.join(conflicts)}")

        name_conflict = check_container_name(entry.subdomain, cfg, root=root)
        if name_conflict:
            raise click.ClickException(
                f"Container name '{entry.subdomain}' conflicts with existing service '{name_conflict}'."
            )

        cfg.projects.entries.append(entry)
        try:
            validated = Config.model_validate(cfg.model_dump(mode="python"))
        except ValidationError as exc:
            raise click.ClickException(f"Invalid project definition: {exc}") from exc
        save_config(validated, config_path(root))

    click.echo(f"✓ Project '{subdomain}' registered at https://{subdomain}.{cfg.domain}")

    # Regenerate typed ingress and portal configuration.
    try:
        generated = run_full_generate(root, cfg)
        artifact_count = sum(len(paths) for paths in generated.values())
        click.echo(f"✓ Generated and validated {artifact_count} deployment artifacts")
    except Exception as exc:
        click.echo(f"⚠ Could not regenerate configs: {exc}")
        click.echo("  Run 'homelab-toolkit projects generate' after correcting the error.")

    click.echo(f"\n  Run 'homelab-toolkit projects deploy {subdomain}' to reconcile the deployment.")
    if auth_mode == "forward_auth":
        click.echo(f"  SSO enabled — access: https://auth.{cfg.domain} (login first)")


@projects.command("remove")
@click.argument("subdomain")
@click.pass_context
def remove_project(ctx, subdomain):
    """Remove a project and its Caddy route."""
    root = Path(ctx.obj["root"])
    from toolkit.cli.config_mutation import cli_configuration_mutation

    with cli_configuration_mutation(root, "project-remove"):
        cfg = load_config(config_path(root))
        before = len(cfg.projects.entries)
        removed = [p for p in cfg.projects.entries if p.subdomain == subdomain]
        cfg.projects.entries = [p for p in cfg.projects.entries if p.subdomain != subdomain]

        if len(cfg.projects.entries) == before:
            raise click.ClickException(f"Project '{subdomain}' not found.")

        save_config(cfg, config_path(root))

    if removed:
        click.echo(f"✓ Removed project '{subdomain}' (was: {removed[0].docker_image})")

    try:
        run_full_generate(root, cfg)
        click.echo("✓ Deployment artifacts regenerated and validated")
    except Exception as exc:
        click.echo(f"⚠ Could not regenerate configs: {exc}")

    click.echo("\n  Run 'homelab-toolkit deploy' to remove the container and apply routing changes.")


@projects.command("generate")
@click.pass_context
def generate_routes(ctx):
    """Regenerate typed ingress and portal configuration."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))

    try:
        generated = run_full_generate(root, cfg)
        artifact_count = sum(len(paths) for paths in generated.values())
        click.echo(f"✓ Generated and validated {artifact_count} deployment artifacts")
    except Exception as exc:
        raise click.ClickException(f"Config generation failed: {exc}")


def _run_runtime_action(ctx: click.Context, subdomain: str, action: str) -> None:
    from typing import cast

    from toolkit.core.projects.runtime import ProjectCommand, run_project_command

    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    result = run_project_command(root, cfg, subdomain, cast(ProjectCommand, action))
    if not result.ok:
        raise click.ClickException(result.output)
    click.echo(result.output)


def _deploy_project_stack(root: Path, subdomain: str) -> bool:
    import asyncio

    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
    from toolkit.core.projects.runtime import find_project

    cfg = load_config(config_path(root))
    if find_project(cfg, subdomain) is None:
        raise click.ClickException(f"Project '{subdomain}' is not registered.")
    result = asyncio.run(
        run_deploy_workflow(
            root,
            cfg,
            on_log=click.echo,
            on_step=lambda step, state: click.echo(f"[{state}] {step}"),
            skip_infra=True,
        )
    )
    return result.success


@projects.command("deploy")
@click.argument("subdomain")
@click.pass_context
def deploy_project(ctx, subdomain):
    """Generate, deploy, and verify the current project desired state."""
    root = Path(ctx.obj["root"])
    if not _deploy_project_stack(root, subdomain):
        raise click.ClickException("Deployment did not converge; inspect the operation log above.")


@projects.command("stop")
@click.argument("subdomain")
@click.pass_context
def stop_project(ctx, subdomain):
    """Stop a managed project container."""
    _run_runtime_action(ctx, subdomain, "stop")


@projects.command("start")
@click.argument("subdomain")
@click.pass_context
def start_project(ctx, subdomain):
    """Start a managed project container."""
    _run_runtime_action(ctx, subdomain, "start")


@projects.command("restart")
@click.argument("subdomain")
@click.pass_context
def restart_project(ctx, subdomain):
    """Restart a managed project container."""
    _run_runtime_action(ctx, subdomain, "restart")


@projects.command("logs")
@click.argument("subdomain")
@click.pass_context
def logs_project(ctx, subdomain):
    """Fetch the latest 200 project log lines."""
    _run_runtime_action(ctx, subdomain, "logs")


@projects.command("status")
@click.argument("subdomain")
@click.pass_context
def status_project(ctx, subdomain):
    """Show the managed project container state."""
    _run_runtime_action(ctx, subdomain, "status")


@projects.command("ps")
@click.pass_context
def ps_projects(ctx):
    """List all managed project container states."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    from toolkit.core.projects.runtime import run_project_command

    if not cfg.projects.entries:
        click.echo("No projects registered.")
        return
    failed = False
    for project in cfg.projects.entries:
        result = run_project_command(root, cfg, project.subdomain, "status")
        marker = "ok" if result.ok else "error"
        click.echo(f"{project.subdomain:<24} {result.node:<8} {marker:<5} {result.output}")
        failed = failed or not result.ok
    if failed:
        raise click.ClickException("One or more project states could not be read.")
