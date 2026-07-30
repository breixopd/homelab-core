"""Operational approval queue CLI."""

from __future__ import annotations

import click

from toolkit.cli import load_root_config


def _approval_store(root):
    from toolkit.core.ops.approvals import ApprovalPersistenceError, ApprovalStore

    try:
        return ApprovalStore(root=root)
    except ApprovalPersistenceError as exc:
        raise click.ClickException(str(exc)) from exc


def _approval_mutation(operation):
    from toolkit.core.ops.approvals import ApprovalPersistenceError

    try:
        return operation()
    except ApprovalPersistenceError as exc:
        raise click.ClickException(str(exc)) from exc


def _execute_rightsize(root, store, approval) -> None:
    from toolkit.core.ops.watchdog.rightsize import RightsizeApplyError, execute_approved_rightsize

    try:
        execute_approved_rightsize(
            root=root,
            approval=approval,
            on_log=lambda message: click.echo(f"  {message}"),
        )
    except RightsizeApplyError as exc:
        failure_detail = str(exc)
        _approval_mutation(lambda: store.record_outcome(approval.id, success=False, detail=failure_detail))
        raise click.ClickException(f"Approved rightsizing could not be applied: {exc}") from exc
    _approval_mutation(lambda: store.record_outcome(approval.id, success=True, detail="applied and verified"))


@click.group("approvals")
def approvals() -> None:
    """Review and execute guarded operational changes."""


@approvals.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit the queue as JSON.")
@click.option("--all", "include_all", is_flag=True, help="Include executed/rejected history (not just pending).")
@click.pass_context
def approvals_list(ctx, as_json: bool, include_all: bool):
    """Show the approval queue."""
    from toolkit.core.ops.approvals import ApprovalStatus

    root, _ = load_root_config(ctx)
    store = _approval_store(root)
    if include_all:
        entries = store.all()
    else:
        entries = store.actionable()

    if as_json:
        import json

        click.echo(json.dumps([a.to_dict() for a in entries], indent=2))
        return

    if not entries:
        click.echo("No actionable approvals." + ("" if include_all else " (use --all for history)"))
        return

    for a in entries:
        click.secho(
            f"  [{a.kind.value}] {a.id}  {a.service}  {a.current}→{a.proposed}",
            fg="yellow",
        )
        if a.reason:
            click.echo(f"        reason: {a.reason}")
        command = "approve" if a.status is ApprovalStatus.REQUESTED else "execute"
        click.echo(f"        requested: {a.requested_by}  next: 'approvals {command} {a.id}'")


@approvals.command("approve")
@click.argument("approval_id")
@click.pass_context
def approvals_approve(ctx, approval_id: str):
    """Approve a pending request by id."""
    from toolkit.core.state.audit_log import AuditAction, audit

    root, _ = load_root_config(ctx)
    store = _approval_store(root)
    a = _approval_mutation(lambda: store.approve(approval_id, decided_by="cli-operator"))
    if a is None:
        raise click.ClickException(f"No pending approval with id {approval_id!r}")
    audit(
        root,
        AuditAction.MANUAL,
        actor="cli-approvals",
        ok=True,
        detail=f"approved {a.kind.value} {a.service} {a.current}→{a.proposed}",
    )
    try:
        _execute_rightsize(root, store, a)
    except click.ClickException as exc:
        audit(
            root,
            AuditAction.MANUAL,
            actor="cli-approvals",
            ok=False,
            detail=f"approved rightsizing failed for {a.service}: {exc}",
        )
        raise
    click.secho(
        f"Approved, applied, and verified rightsizing for {a.service} ({a.current}→{a.proposed})",
        fg="green",
    )


@approvals.command("execute")
@click.argument("approval_id")
@click.pass_context
def approvals_execute(ctx, approval_id: str):
    """Execute an approved request left pending after an interruption."""
    from toolkit.core.ops.approvals import ApprovalKind, ApprovalStatus

    root, _ = load_root_config(ctx)
    store = _approval_store(root)
    approval = store.find(approval_id)
    if approval is None or approval.status is not ApprovalStatus.APPROVED:
        raise click.ClickException(f"No executable approval with id {approval_id!r}")
    if approval.kind is not ApprovalKind.RIGHTSIZE:
        raise click.ClickException("Approval has no executor")
    _execute_rightsize(root, store, approval)
    click.secho(f"Applied and verified rightsizing for {approval.service}", fg="green")


@approvals.command("reject")
@click.argument("approval_id")
@click.option("--reason", default="", help="Optional rejection reason.")
@click.pass_context
def approvals_reject(ctx, approval_id: str, reason: str):
    """Reject a pending request by id."""
    from toolkit.core.state.audit_log import AuditAction, audit

    root, _ = load_root_config(ctx)
    store = _approval_store(root)
    a = _approval_mutation(lambda: store.reject(approval_id, decided_by="cli-operator", reason=reason))
    if a is None:
        raise click.ClickException(f"No pending approval with id {approval_id!r}")
    audit(
        root,
        AuditAction.MANUAL,
        actor="cli-approvals",
        ok=True,
        detail=f"rejected {a.kind.value} {a.service} ({reason})",
    )
    click.secho(f"✓ Rejected {a.kind.value} for {a.service}", fg="red")
