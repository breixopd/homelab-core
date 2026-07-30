from __future__ import annotations

import uuid
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import click

from toolkit.core.config.config import load_config
from toolkit.core.config.storage import config_path
from toolkit.core.ops.notifications import send_email, send_ntfy
from toolkit.core.ops.updates import UpdateCheckError, run_framework_check


def _updates_url(domain: str) -> str:
    return f"https://homelab.{domain}/operations#updates-heading"


def _config_dict(root: Path) -> dict:
    """Load config.yaml as a plain dict (for notification flag checks)."""
    cfg = load_config(config_path(root))
    return cfg.model_dump() if hasattr(cfg, "model_dump") else {}


# ── ntfy notification (enhanced) ──────────────────────────────────────────────


def _send_ntfy_notification(
    root: Path,
    service_outdated: list[dict],
    framework_updates: list[dict],
) -> None:
    """Send ntfy notification with markdown body and click action."""
    cfg_dict = _config_dict(root)
    notify_cfg = cfg_dict.get("notifications", {})

    # Check if ntfy is enabled in config
    if not notify_cfg.get("update_check_ntfy", True):
        return

    total = len(service_outdated) + len(framework_updates)
    domain = cfg_dict.get("domain", "")
    updates_url = _updates_url(domain) if domain else ""

    # Build markdown body
    lines = [f"### Homelab: {total} update(s) available"]
    lines.append("")
    if service_outdated:
        lines.append("**Service updates:**")
        for r in sorted(service_outdated, key=lambda x: x["service"]):
            changelog = r.get("changelog_url", "")
            cl = f" ([changelog]({changelog}))" if changelog else ""
            lines.append(f"- {r['service']}: `{r['current']}` → `{r['latest']}`{cl}")
        lines.append("")

    if framework_updates:
        lines.append("**Framework dependency updates:**")
        for r in sorted(framework_updates, key=lambda x: x["name"]):
            latest = r["latest"] if r["latest"] else "?"
            lines.append(f"- {r['name']}: `{r['current']}` → `{latest}`")
        lines.append("")

    if updates_url:
        lines.append(f"[Review & approve]({updates_url})")

    body = "\n".join(lines)
    extra_headers: dict[str, str] = {"X-Markdown": "true"}
    if updates_url:
        extra_headers["Click"] = updates_url
        extra_headers["X-Click"] = updates_url
    send_ntfy(
        body,
        f"Homelab: {total} update(s) available",
        "default",
        root,
        tags="package",
        extra_headers=extra_headers,
    )


# ── email notification ────────────────────────────────────────────────────────


# Shared inline styles for the HTML update email (email clients need inline CSS).
_TD = "padding:10px 12px;border-bottom:1px solid #e2e8f0;"
_TD_MONO = f"{_TD}font-family:monospace;font-size:13px;"
_TH = "padding:10px 12px;font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;"
_TABLE = (
    "width:100%;border-collapse:collapse;background:#fff;border-radius:8px;"
    "overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);"
)
_H3 = "color:#1e293b;font-size:16px;margin:24px 0 12px;"
_BTN = (
    "display:inline-block;padding:10px 24px;background:#6366f1;color:#fff;"
    "text-decoration:none;border-radius:6px;font-size:14px;font-weight:600;"
)
_BTN_SM = (
    "display:inline-block;padding:5px 14px;background:#6366f1;color:#fff;"
    "text-decoration:none;border-radius:4px;font-size:12px;font-weight:600;"
)


def _public_https_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2_048 or any(ord(character) < 32 for character in value):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return escape(value, quote=True)


def _email_table(title: str, headers: list[str], body_rows: list[str]) -> str:
    ths = "".join(
        f'<th style="{_TH}text-align:{"center" if h in ("Changelog", "Source", "Action") else "left"};">{h}</th>'
        for h in headers
    )
    return f"""<h3 style="{_H3}">{title}</h3>
<table cellpadding="0" cellspacing="0" style="{_TABLE}">
    <thead><tr style="background:#f8fafc;">{ths}</tr></thead>
    <tbody>{"".join(body_rows)}</tbody>
</table>"""


def _build_email_html(
    service_outdated: list[dict],
    framework_updates: list[dict],
    domain: str,
    updates_url: str,
) -> str:
    """Build professional HTML email body with update tables."""
    total = len(service_outdated) + len(framework_updates)
    safe_updates_url = _public_https_url(updates_url)
    review_action = (
        f'<a href="{safe_updates_url}" style="{_BTN_SM}">Review</a>'
        if safe_updates_url
        else '<span style="color:#94a3b8;">Unavailable</span>'
    )

    rows: list[str] = []
    for r in sorted(service_outdated, key=lambda x: x["service"]):
        changelog = _public_https_url(r.get("changelog_url", ""))
        cl_link = (
            f'<a href="{changelog}" style="color:#6366f1;text-decoration:none;">View</a>'
            if changelog
            else '<span style="color:#94a3b8;">—</span>'
        )
        rows.append(f"""<tr>
            <td style="{_TD_MONO}">{escape(str(r["service"]))}</td>
            <td style="{_TD_MONO}color:#64748b;">{escape(str(r["current"]))}</td>
            <td style="{_TD_MONO}color:#059669;font-weight:600;">{escape(str(r["latest"]))}</td>
            <td style="{_TD}text-align:center;">{cl_link}</td>
            <td style="{_TD}text-align:center;">{review_action}</td>
        </tr>""")

    framework_rows: list[str] = []
    for r in sorted(framework_updates, key=lambda x: x["name"]):
        latest = r["latest"] if r["latest"] else "?"
        framework_rows.append(f"""<tr>
            <td style="{_TD_MONO}">{escape(str(r["name"]))}</td>
            <td style="{_TD_MONO}color:#64748b;">{escape(str(r["current"]))}</td>
            <td style="{_TD_MONO}color:#059669;font-weight:600;">{escape(str(latest))}</td>
            <td style="{_TD_MONO}color:#94a3b8;">{escape(str(r["source"]))}</td>
            <td style="{_TD}text-align:center;"><span style="color:#94a3b8;font-size:12px;">CLI</span></td>
        </tr>""")

    svc_section = ""
    if service_outdated:
        svc_section = _email_table(
            f"Service Updates ({len(service_outdated)})",
            ["Service", "Current", "Latest", "Changelog", "Action"],
            rows,
        )

    framework_section = ""
    if framework_updates:
        framework_section = _email_table(
            f"Framework Dependencies ({len(framework_updates)})",
            ["Name", "Current", "Latest", "Source", "Action"],
            framework_rows,
        )

    body_font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
    updates_action = (
        f"""<tr>
        <td style="padding:24px 0 0;text-align:center;">
            <a href="{safe_updates_url}" style="{_BTN}">View All Updates</a>
        </td>
    </tr>"""
        if safe_updates_url
        else ""
    )
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:{body_font};">
<table cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;margin:0 auto;padding:32px 16px;">
    <tr>
        <td style="text-align:center;padding-bottom:24px;">
            <h1 style="color:#1e293b;font-size:22px;margin:0;">🚀 Homelab Updates</h1>
            <p style="color:#64748b;font-size:14px;margin:6px 0 0;">{total} update(s) available for your homelab</p>
        </td>
    </tr>
    {svc_section}
    {framework_section}
    {updates_action}
    <tr>
        <td style="padding:24px 0 0;text-align:center;color:#94a3b8;font-size:12px;">
            <p style="margin:0;">This is an automated nightly update check from your homelab toolkit.</p>
            <p style="margin:4px 0 0;">{escape(domain)} &middot; Nightly scan</p>
        </td>
    </tr>
</table>
</body>
</html>"""
    return html


def _send_email_notification(
    root: Path,
    service_outdated: list[dict],
    framework_updates: list[dict],
) -> None:
    """Send email notification via SMTP if configured."""
    cfg_dict = _config_dict(root)
    notify_cfg = cfg_dict.get("notifications", {})

    if not notify_cfg.get("update_check_email", True):
        return

    total = len(service_outdated) + len(framework_updates)
    domain = cfg_dict.get("domain", "")
    updates_url = _updates_url(domain) if domain else ""

    html_body = _build_email_html(service_outdated, framework_updates, domain, updates_url)
    subject = f"Homelab: {total} update(s) available for {domain}"
    plain = f"{total} update(s) available."
    if updates_url:
        plain += f" View at {updates_url}"
    send_email(subject, plain, root, html_body=html_body)


# ── CLI commands ─────────────────────────────────────────────────────────────


@click.group()
def update():
    """Discover, apply, and roll back verified releases."""


def _submit_update_job(ctx: click.Context, operation):
    from toolkit.cli import load_controller_client
    from toolkit.cli.controller_jobs import wait_for_controller_job
    from toolkit.controller.client import ControllerClientError
    from toolkit.controller.contracts import JobRequest

    try:
        client = load_controller_client(ctx)
        queued = client.submit(JobRequest(idempotency_key=f"update-{uuid.uuid4().hex}", operation=operation))
        click.echo(f"Queued controller job {queued.job_id}")

        def show_event(event) -> None:
            stage = event.payload.get("stage")
            suffix = f" ({stage})" if isinstance(stage, str) and stage else ""
            click.echo(f"  [{event.level:<7}] {event.message}{suffix}")

        job = wait_for_controller_job(client, queued.job_id, on_event=show_event, timeout_seconds=4 * 60 * 60)
    except ControllerClientError as exc:
        raise click.ClickException(str(exc)) from exc
    if job.state.value != "SUCCEEDED":
        message = job.error.message if job.error else "Update operation did not complete"
        raise click.ClickException(message)
    return client, job


@update.command("check")
@click.option("--notify/--no-notify", default=None, help="Send notifications (email + ntfy)")
@click.option("--email/--no-email", default=None, help="Send email notification")
@click.option("--ntfy/--no-ntfy", default=None, help="Send ntfy notification")
@click.pass_context
def check_updates(
    ctx: click.Context,
    notify: bool | None,
    email: bool | None,
    ntfy: bool | None,
):
    """Check verified service releases and framework dependencies."""
    root = Path(ctx.obj["root"])
    cfg_dict = _config_dict(root)
    notify_cfg = cfg_dict.get("notifications", {})

    # Resolve notification flags: explicit flags override config
    if email is not None:
        send_email = email
    elif notify is not None:
        send_email = notify
    else:
        send_email = notify_cfg.get("update_check_email", True)
    if ntfy is not None:
        send_ntfy = ntfy
    elif notify is not None:
        send_ntfy = notify
    else:
        send_ntfy = notify_cfg.get("update_check_ntfy", True)

    from toolkit.controller.contracts import UpdateOperation

    click.echo("Checking registries for compatible updates...")
    client, _job = _submit_update_job(ctx, UpdateOperation(action="refresh"))
    view = client.operations_view().updates
    service_outdated = [
        {
            "service": candidate.service,
            "current": candidate.current,
            "latest": candidate.target,
            "changelog_url": candidate.changelog_url,
        }
        for candidate in view.candidates
    ]
    up_to_date_svc: list[dict] = []

    click.echo("  Framework dependencies...")
    try:
        framework_report = run_framework_check(root)
    except UpdateCheckError as exc:
        raise click.ClickException(str(exc)) from exc
    framework_with_latest = [r for r in framework_report if r.get("latest")]
    framework_without_latest = [r for r in framework_report if not r.get("latest")]

    if service_outdated:
        click.echo()
        click.echo(f"{'Available Service Updates':━^72}")
        click.echo(f"{'Service':<24} {'Current':<20} {'Latest':<20}")
        click.echo("─" * 64)
        for r in sorted(service_outdated, key=lambda x: x["service"]):
            click.echo(f"{r['service']:<24} {r['current']:<20} {r['latest']:<20}")
        click.echo()

    if up_to_date_svc:
        click.echo(f"Up-to-date services ({len(up_to_date_svc)}):")
        for r in sorted(up_to_date_svc, key=lambda x: x["service"]):
            click.echo(f"  \u2713 {r['service']:<22} {r['current']}")
        click.echo()

    if framework_with_latest:
        click.echo(f"{'Available Framework Dependency Updates':━^72}")
        click.echo(f"{'Item':<36} {'Current':<20} {'Latest':<20}")
        click.echo("─" * 72)
        for r in sorted(framework_with_latest, key=lambda x: x["name"]):
            click.echo(f"{r['name']:<36} {r['current']:<20} {r['latest']:<20}")
        click.echo()

    if framework_without_latest:
        click.echo(f"Framework dependencies ({len(framework_without_latest)}):")
        for r in sorted(framework_without_latest, key=lambda x: x["name"]):
            click.echo(f"  \u2022 {r['name']:<34} {r['current']}")
        click.echo()

    total_svc = len(service_outdated) + len(up_to_date_svc)
    total_framework = len(framework_report)
    click.echo(f"Services: {total_svc} images, {len(service_outdated)} outdated, {len(up_to_date_svc)} up-to-date")
    click.echo(f"Framework: {total_framework} dependency update(s) available")

    # Send notifications if there are updates
    has_updates = bool(service_outdated) or bool(framework_with_latest)

    if has_updates:
        if send_ntfy:
            click.echo("  Sending ntfy notification...")
            _send_ntfy_notification(root, service_outdated, framework_with_latest)

        if send_email:
            click.echo("  Sending email notification...")
            _send_email_notification(root, service_outdated, framework_with_latest)
    else:
        click.echo("No updates found.")


@update.command("apply")
@click.argument("service", required=False, default=None)
@click.option("--all", "apply_all", is_flag=True, help="Apply every compatible update in the active plan")
@click.pass_context
def apply_update(ctx: click.Context, service: str | None, apply_all: bool):
    """Apply digest-pinned updates with verification and automatic rollback."""
    if not service and not apply_all:
        raise click.UsageError("Specify SERVICE or use --all")
    from toolkit.cli import load_controller_client
    from toolkit.controller.contracts import UpdateOperation

    view = load_controller_client(ctx).operations_view().updates
    if view.recovery_required:
        raise click.ClickException(
            "Release recovery is required before applying updates; run 'homelab-toolkit update recover'"
        )
    available = {candidate.service for candidate in view.candidates}
    selected = sorted(available) if apply_all else [service] if service in available else []
    if not selected:
        raise click.ClickException("Selected service is not in the active update plan")
    _submit_update_job(
        ctx,
        UpdateOperation(action="apply", services=selected, revision=view.revision),
    )
    click.secho("Verified release applied.", fg="green")


@update.command("diff")
@click.argument("service")
@click.pass_context
def show_diff(ctx: click.Context, service: str):
    """Show the active plan entry and upstream release notes."""
    from toolkit.cli import load_controller_client

    for candidate in load_controller_client(ctx).operations_view().updates.candidates:
        if candidate.service == service:
            if candidate.changelog_url:
                click.echo(f"Changelog for {service}:")
                click.echo(f"  {candidate.changelog_url}")
            else:
                click.echo(f"No changelog URL available for {service}.")
            click.echo(f"  Current: {candidate.current}")
            click.echo(f"  Target:  {candidate.target}")
            return
    raise click.ClickException(f"Service '{service}' is not in the active update plan")


@update.command("rollback")
@click.pass_context
def rollback(ctx: click.Context):
    """Restore the previous verified release."""
    from toolkit.cli import load_controller_client
    from toolkit.controller.contracts import UpdateOperation

    view = load_controller_client(ctx).operations_view().updates
    if not view.rollback_available or not view.active_revision:
        raise click.ClickException("No rollback release is available")
    _submit_update_job(ctx, UpdateOperation(action="rollback", revision=view.active_revision))
    click.secho("Previous release restored and verified.", fg="green")


@update.command("recover")
@click.pass_context
def recover(ctx: click.Context):
    """Complete a previously failed automatic rollback and verify the release."""
    from toolkit.cli import load_controller_client
    from toolkit.controller.contracts import UpdateOperation

    view = load_controller_client(ctx).operations_view().updates
    if not view.recovery_required:
        raise click.ClickException("No release recovery is required")
    _submit_update_job(ctx, UpdateOperation(action="recover"))
    click.secho("Previous release recovered and verified.", fg="green")
