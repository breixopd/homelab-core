from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

import click

from toolkit.cli.context import load_controller_client as load_controller_client
from toolkit.cli.context import load_root_config as load_root_config
from toolkit.controller.client import controller_client_from_environment
from toolkit.core.compose.registry import load_all
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, resolve_homelab_root


@click.group()
@click.option(
    "--root",
    envvar="HOMELAB_ROOT",
    default=DEFAULT_HOMELAB_ROOT,
    help="Homelab repo root (config.yaml, generated/, docker-compose.yml)",
)
@click.pass_context
def main(ctx: click.Context, root: str):
    """homelab-toolkit — Self-hosted infrastructure management."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = str(resolve_homelab_root(root, prefer_cwd=True))
    ctx.obj["controller_factory"] = controller_client_from_environment
    load_all()


# Import and register subcommands
from toolkit.cli.approvals_cmd import approvals  # noqa: E402
from toolkit.cli.bootstrap_cmd import bootstrap  # noqa: E402
from toolkit.cli.config_cmd import config  # noqa: E402
from toolkit.cli.deploy_cmd import deploy  # noqa: E402
from toolkit.cli.dns_cmd import dns  # noqa: E402
from toolkit.cli.fleet_cmd import fleet  # noqa: E402
from toolkit.cli.generate_cmd import generate  # noqa: E402
from toolkit.cli.health import health  # noqa: E402
from toolkit.cli.images_cmd import images  # noqa: E402
from toolkit.cli.install_cmd import install  # noqa: E402
from toolkit.cli.ldap_cmd import ldap  # noqa: E402
from toolkit.cli.machines_cmd import machines  # noqa: E402
from toolkit.cli.maintenance_cmd import maintenance  # noqa: E402
from toolkit.cli.mesh_cmd import mesh  # noqa: E402
from toolkit.cli.ops_cmd import ops  # noqa: E402
from toolkit.cli.projects_cmd import projects  # noqa: E402
from toolkit.cli.secrets_cmd import secrets  # noqa: E402
from toolkit.cli.services import services  # noqa: E402
from toolkit.cli.status_cmd import status_cmd as status  # noqa: E402
from toolkit.cli.update import update  # noqa: E402
from toolkit.cli.users_cmd import users  # noqa: E402
from toolkit.cli.watchdog_cmd import watchdog  # noqa: E402

# Most-used first (status + deploy cycle), then management, network/infra,
# then low-level recovery/internals at the bottom.
main.add_command(ops)  # quick status — what needs doing
main.add_command(status)  # one-view cluster state
main.add_command(deploy)  # deploy all / verify / reconcile / hooks
main.add_command(services)  # start/stop/restart/logs/status
main.add_command(update)  # image updates + rollback
main.add_command(watchdog)  # health monitor + timer + history (audit)
main.add_command(approvals)  # guarded rightsizing review and execution
main.add_command(health)  # full system health
main.add_command(maintenance)  # dumps + uptime + vault sync + cleanup
main.add_command(bootstrap)  # secure first-run setup capability
main.add_command(config)  # edit config.yaml values
main.add_command(secrets)  # rotate / view secrets
main.add_command(users)  # LLDAP user directory
main.add_command(projects)  # subdomain projects (Caddy routes, DNS)
main.add_command(generate)  # regen .env / configs (normally auto)
main.add_command(install)  # first-run wizard
main.add_command(fleet)  # external servers / Komodo / mesh onboarding
main.add_command(mesh)  # Headscale join/router/doctor
main.add_command(dns)  # Cloudflare DNS sync
main.add_command(images)  # custom image build/push/sync
main.add_command(machines)  # desired-state and remote machine management
# Hidden — recovery only; automated by deploy hooks and fleet onboard
main.add_command(ldap)


@main.command("version")
def version_cmd():
    """Show toolkit version."""
    try:
        pkg_version = distribution_version("homelab-toolkit")
    except PackageNotFoundError:
        project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            configured = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]["version"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            configured = "unknown"
        pkg_version = configured if isinstance(configured, str) and configured else "unknown"
    click.echo(f"homelab-toolkit {pkg_version}")


@main.command()
@click.option("--port", default=8080, help="Web UI port")
@click.pass_context
def ui(ctx, port):
    """Launch the Web UI (FastAPI + htmx)."""
    import os
    import subprocess
    import sys

    root = ctx.obj["root"]
    env = os.environ.copy()
    env["HOMELAB_ROOT"] = str(Path(root).absolute())
    env["HOMELAB_UI_PORT"] = str(port)

    try:
        subprocess.run([sys.executable, "-m", "toolkit.webui"], env=env, check=True)
    except KeyboardInterrupt:
        pass
