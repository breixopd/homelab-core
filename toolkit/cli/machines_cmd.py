"""Desired-state and remote-access commands for managed machines."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    DesiredStateValidationError,
    create_machine,
    read_machines_view,
    remove_machine,
    update_machine,
)
from toolkit.controller.read_models import MachineCreate, MachineRemove, MachineUpdate
from toolkit.core.config.mutations import ConfigurationBusyError
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
from toolkit.core.machines import MachineSpec, load_machine_templates


@click.group()
def machines() -> None:
    """Manage machine desired state and remote access."""


def _root(ctx: click.Context) -> Path:
    return Path(ctx.obj["root"])


def _machine_spec(path: Path) -> MachineSpec:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("machine definition must contain a mapping")
        return MachineSpec.model_validate(document)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Invalid machine definition: {exc}") from exc


def _regenerate(root: Path) -> None:
    from toolkit.core.generate.generate import run_full_generate

    click.echo("Validating desired state and regenerating artifacts...")
    try:
        result = run_full_generate(root, validate=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(f"Machine saved, but artifact generation failed: {exc}") from exc
    click.secho(f"Generated {sum(len(paths) for paths in result.values())} validated artifacts.", fg="green")


def _mutation_error(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc) or "Machine update was rejected")


_MUTATION_ERRORS = (
    ConfigurationBusyError,
    DesiredStateConflictError,
    DesiredStateValidationError,
    ValidationError,
    ValueError,
)


@machines.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def list_machines(ctx: click.Context, as_json: bool) -> None:
    """List configured machines, placement impact, and removal state."""
    view = read_machines_view(_root(ctx))
    if as_json:
        click.echo(view.model_dump_json(indent=2))
        return
    from toolkit.cli._format import echo_table

    rows = [
        (
            item.machine_id,
            item.spec.kind.upper(),
            item.spec.hostname,
            item.spec.address,
            "enabled" if item.spec.enabled else "disabled",
            "managed" if item.spec.managed else "external",
            len(item.services),
        )
        for item in view.machines
    ]
    echo_table(rows, columns=("ID", "Kind", "Hostname", "Address", "State", "Ownership", "Services"))


@machines.command("show")
@click.argument("machine_id")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def show_machine(ctx: click.Context, machine_id: str, as_json: bool) -> None:
    """Show one machine and its placement impact."""
    view = read_machines_view(_root(ctx))
    machine = next((item for item in view.machines if item.machine_id == machine_id), None)
    if machine is None:
        raise click.ClickException(f"Unknown machine: {machine_id}")
    if as_json:
        click.echo(machine.model_dump_json(indent=2))
        return
    from toolkit.cli._format import echo_panel

    body = (
        f"Kind:      {machine.spec.kind.upper()}\n"
        f"Hostname:  {machine.spec.hostname}\n"
        f"Address:   {machine.spec.address}/{machine.spec.cidr}\n"
        f"VMID:      {machine.spec.vmid}\n"
        f"Resources: {machine.spec.cores} CPU, {machine.spec.memory_mb} MB, {machine.spec.root_disk_gb} GB\n"
        f"Labels:    {', '.join(machine.spec.labels) or '-'}\n"
        f"Services:  {', '.join(machine.services) or '-'}\n"
        f"Projects:  {', '.join(machine.projects) or '-'}\n"
        f"Removal:   {', '.join(machine.removal_blockers) or 'ready'}"
    )
    echo_panel(title=machine.machine_id, body=body)


@machines.command("add")
@click.argument("machine_id")
@click.option("--file", "definition", type=click.Path(exists=True, dir_okay=False))
@click.option("--template", "template_id", help="Use a discovered machine template.")
@click.pass_context
def add_machine(ctx: click.Context, machine_id: str, definition: str | None, template_id: str | None) -> None:
    """Add a machine from a strict definition file or discovered template."""
    if bool(definition) == bool(template_id):
        raise click.ClickException("Specify exactly one of --file or --template")
    if definition is not None:
        spec = _machine_spec(Path(definition))
    else:
        templates = load_machine_templates(_root(ctx))
        try:
            spec = templates[str(template_id)]
        except KeyError as exc:
            raise click.ClickException(f"Unknown machine template: {template_id}") from exc
    root = _root(ctx)
    view = read_machines_view(root)
    try:
        create_machine(
            root,
            MachineCreate(expected_revision=view.revision, machine_id=machine_id, spec=spec),
        )
    except _MUTATION_ERRORS as exc:
        raise _mutation_error(exc) from exc
    click.secho(f"Added machine {machine_id}.", fg="green")
    _regenerate(root)


@machines.command("update")
@click.argument("machine_id")
@click.option(
    "--file",
    "definition",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.pass_context
def update_machine_command(ctx: click.Context, machine_id: str, definition: str) -> None:
    """Replace one machine definition from a strict YAML file."""
    root = _root(ctx)
    view = read_machines_view(root)
    try:
        update_machine(
            root,
            machine_id,
            MachineUpdate(expected_revision=view.revision, spec=_machine_spec(Path(definition))),
        )
    except _MUTATION_ERRORS as exc:
        raise _mutation_error(exc) from exc
    click.secho(f"Updated machine {machine_id}.", fg="green")
    _regenerate(root)


def _set_enabled(ctx: click.Context, machine_id: str, *, enabled: bool) -> None:
    root = _root(ctx)
    view = read_machines_view(root)
    current = next((item for item in view.machines if item.machine_id == machine_id), None)
    if current is None:
        raise click.ClickException(f"Unknown machine: {machine_id}")
    try:
        update_machine(
            root,
            machine_id,
            MachineUpdate(
                expected_revision=view.revision,
                spec=current.spec.model_copy(update={"enabled": enabled}),
            ),
        )
    except _MUTATION_ERRORS as exc:
        raise _mutation_error(exc) from exc
    click.secho(f"{machine_id} is now {'enabled' if enabled else 'disabled'}.", fg="green")
    _regenerate(root)


@machines.command("enable")
@click.argument("machine_id")
@click.pass_context
def enable_machine(ctx: click.Context, machine_id: str) -> None:
    """Enable a configured machine."""
    _set_enabled(ctx, machine_id, enabled=True)


@machines.command("disable")
@click.argument("machine_id")
@click.pass_context
def disable_machine(ctx: click.Context, machine_id: str) -> None:
    """Disable a machine after moving all dependent placements."""
    _set_enabled(ctx, machine_id, enabled=False)


@machines.command("remove")
@click.argument("machine_id")
@click.option("--yes", is_flag=True, help="Confirm removal without prompting.")
@click.pass_context
def remove_machine_command(ctx: click.Context, machine_id: str, yes: bool) -> None:
    """Remove a disabled, unmanaged, unreferenced machine definition."""
    if not yes:
        click.confirm(f"Remove machine definition {machine_id}?", abort=True)
    root = _root(ctx)
    view = read_machines_view(root)
    try:
        remove_machine(
            root,
            machine_id,
            MachineRemove(
                expected_revision=view.revision,
                machine_id=machine_id,
                confirmation=machine_id,
            ),
        )
    except _MUTATION_ERRORS as exc:
        raise _mutation_error(exc) from exc
    click.secho(f"Removed machine definition {machine_id}.", fg="green")
    _regenerate(root)


@machines.command("retire")
@click.argument("machine_id")
@click.option("--yes", is_flag=True, help="Approve the displayed machine retirement plan.")
@click.pass_context
def retire_machine_command(ctx: click.Context, machine_id: str, yes: bool) -> None:
    """Destroy and remove one unplaced managed machine through guarded approval."""
    import uuid

    from toolkit.cli import load_controller_client
    from toolkit.cli.controller_jobs import wait_for_controller_job
    from toolkit.controller.client import ControllerClientError
    from toolkit.controller.contracts import DestroyInfraOperation, DestroyPlanRequest, JobRequest

    confirmation = f"RETIRE MACHINE {machine_id}"
    try:
        client = load_controller_client(ctx)
        plan = client.create_destruction_plan(
            DestroyPlanRequest(action="retire_machine", scopes=[machine_id]),
        )
        click.echo("Machine retirement plan")
        click.echo(f"  Machine:    {machine_id}")
        click.echo(f"  Plan:       {plan.plan_id}")
        click.echo(f"  Checkpoint: {plan.spec.checkpoint_id} ({plan.spec.checkpoint_verified_at.isoformat()})")
        click.echo(f"  Revision:   {plan.spec.config_revision}")
        click.echo(f"  Plan hash:  {plan.plan_hash}")
        if not yes:
            supplied = click.prompt(f'Type "{confirmation}" to continue', default="", show_default=False)
            if supplied != confirmation:
                raise click.Abort()
        approval = client.approve_plan(
            plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation=confirmation,
        )
        queued = client.submit(
            JobRequest(
                idempotency_key=f"retire-{uuid.uuid4().hex}",
                operation=DestroyInfraOperation(
                    action=plan.spec.action,
                    scopes=plan.spec.scopes,
                    config_revision=plan.spec.config_revision,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    approval_token=approval.token,
                ),
            )
        )
        click.echo(f"Queued controller job {queued.job_id}")

        def show_event(event) -> None:
            stage = event.payload.get("stage")
            suffix = f" ({stage})" if isinstance(stage, str) and stage else ""
            click.echo(f"  [{event.level:<7}] {event.message}{suffix}")

        finished = wait_for_controller_job(client, queued.job_id, on_event=show_event)
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc
    if finished.state.value != "SUCCEEDED":
        message = finished.error.message if finished.error else "Machine retirement did not complete"
        raise click.ClickException(message)
    click.secho(f"Machine {machine_id} retired and independently verified.", fg="green")


@machines.command("sync")
@click.argument("machine_id")
@click.pass_context
def sync_machine(ctx: click.Context, machine_id: str) -> None:
    """Synchronize repository artifacts to a managed machine."""
    from toolkit.core.deploy.repo_sync import sync_repo_to_role

    root = _root(ctx)
    try:
        sync_repo_to_role(root, machine_id)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Synced homelab files to {machine_id} at {DEFAULT_HOMELAB_ROOT}")


@machines.command("parity")
@click.option("--machine", "machine_id", default=None, help="Restrict the check to one machine ID.")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON.")
@click.pass_context
def parity_machine(ctx: click.Context, machine_id: str | None, as_json: bool) -> None:
    """Verify managed machines run the controller's repository revision."""
    from toolkit.core.deploy.repo_parity import format_parity_report, verify_repo_parity

    try:
        results = verify_repo_parity(_root(ctx), vm_name=machine_id)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "machine": result.vm,
                        "controller_sha": result.controller_sha,
                        "expected_sha": result.expected_sha,
                        "guest_sha": result.guest_sha,
                        "in_parity": result.in_parity,
                        "detail": result.detail,
                    }
                    for result in results
                ],
                indent=2,
            )
        )
    else:
        click.echo(format_parity_report(results))
    if not all(result.in_parity for result in results):
        ctx.exit(1)


def _enabled_machine(ctx: click.Context, machine_id: str):
    from toolkit.cli import load_root_config

    _, cfg = load_root_config(ctx)
    try:
        return cfg, cfg.machines[machine_id], cfg.node_ip(machine_id)
    except KeyError as exc:
        raise click.ClickException(f"Unknown or disabled machine: {machine_id}") from exc


@machines.command("exec")
@click.argument("machine_id")
@click.argument("command", nargs=-1, required=True)
@click.pass_context
def exec_machine(ctx: click.Context, machine_id: str, command: tuple[str, ...]) -> None:
    """Run a command through verified SSH on a managed machine."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    cfg, _machine, ip = _enabled_machine(ctx, machine_id)
    code, stdout, stderr = ssh_run_on_vm(
        cfg,
        ip,
        shlex.join(command),
        root=_root(ctx),
        timeout=cfg.ssh.command_timeout,
    )
    if stdout:
        click.echo(stdout, nl=not stdout.endswith("\n"))
    if code:
        raise click.ClickException((stderr or f"remote command exited with status {code}").strip())


@machines.command("push")
@click.argument("machine_id")
@click.argument("local", type=click.Path(exists=True))
@click.argument("remote")
@click.pass_context
def push_machine(ctx: click.Context, machine_id: str, local: str, remote: str) -> None:
    """Copy a local file or directory to a managed machine."""
    from toolkit.core.ansible.ansible_ssh import scp_to_vm

    cfg, _machine, ip = _enabled_machine(ctx, machine_id)
    try:
        scp_to_vm(cfg, _root(ctx), Path(local), ip, remote)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@machines.command("pull")
@click.argument("machine_id")
@click.argument("remote")
@click.argument("local", type=click.Path())
@click.pass_context
def pull_machine(ctx: click.Context, machine_id: str, remote: str, local: str) -> None:
    """Copy a remote path from a managed machine."""
    from toolkit.core.ansible.ansible_ssh import scp_from_vm

    cfg, _machine, ip = _enabled_machine(ctx, machine_id)
    try:
        scp_from_vm(cfg, _root(ctx), ip, remote, Path(local))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@machines.command("rsync")
@click.argument("machine_id")
@click.pass_context
def rsync_machine(ctx: click.Context, machine_id: str) -> None:
    """Synchronize the toolkit directory to a managed machine."""
    from toolkit.core.ansible.ansible_ssh import ssh_argv

    cfg, machine, ip = _enabled_machine(ctx, machine_id)
    root = _root(ctx)
    try:
        remote_shell = shlex.join(ssh_argv(cfg, root, ip))
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    subprocess.run(
        [
            "rsync",
            "-avz",
            "--delete",
            "-e",
            remote_shell,
            f"{root / 'toolkit'}/",
            f"{machine.effective_ssh_user}@{ip}:{DEFAULT_HOMELAB_ROOT}/toolkit/",
        ],
        check=True,
    )
