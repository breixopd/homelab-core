"""CLI commands for health reporting (homelab-toolkit health)."""

from __future__ import annotations

from pathlib import Path

import click

from toolkit.cli import load_root_config
from toolkit.core.ops.notifications import send_email, send_ntfy


@click.group()
def health():
    """Comprehensive system health reporting."""


@health.command("report")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--markdown", "as_markdown", is_flag=True, help="Output as Markdown")
@click.option("--html", "as_html", is_flag=True, help="Output as HTML email body")
@click.option("--notify", is_flag=True, help="Send notification via ntfy")
@click.option("--email", is_flag=True, help="Send notification via email")
@click.option("--save", default=None, type=click.Path(), help="Save report to file path")
@click.pass_context
def report_cmd(
    ctx: click.Context,
    as_json: bool,
    as_markdown: bool,
    as_html: bool,
    notify: bool,
    email: bool,
    save: str | None,
):
    """Run a comprehensive daily health report.

    Collects system health across 7 dimensions (updates, containers,
    resources, storage, certificates, security, maintenance) and produces
    a structured report in the requested format.
    """
    root, cfg = load_root_config(ctx)

    from toolkit.core.ops.health_report import create_health_report

    click.echo("Collecting health data...", err=True)
    report = create_health_report(root, cfg)

    # Choose format
    if as_json:
        output = report.format_json()
    elif as_markdown:
        output = report.format_markdown()
    elif as_html:
        output = report.format_html()
    else:
        output = report.format_text()

    click.echo(output)

    # Save to file
    if save:
        path = Path(save)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            path.write_text(report.format_json())
        elif path.suffix == ".html":
            path.write_text(report.format_html())
        elif path.suffix == ".md":
            path.write_text(report.format_markdown())
        else:
            path.write_text(report.format_text())
        click.echo(f"Report saved to {path}", err=True)

    # Send notifications
    if notify:
        _send_ntfy(root, report)

    if email:
        _send_email(root, report)

    # Exit with non-zero if issues found (for scripting)
    if report.has_issues() and not as_json and not as_markdown and not as_html:
        click.echo("\n\u26a0  Issues detected \u2014 check the report above for details.", err=True)


# ── ntfy notification ─────────────────────────────────────────────────────


def _send_ntfy(root: Path, report) -> None:
    """Send a health report via ntfy."""
    body = report.format_markdown()
    total_issues = sum(
        [
            report.updates.total,
            len(report.containers.unhealthy),
            len(report.containers.restarting),
        ]
    )
    title = f"Homelab Health: {total_issues} issue(s)" if report.has_issues() else "Homelab Health: OK"
    priority = "high" if report.has_issues() else "default"
    tags = "warning" if report.has_issues() else "white_check_mark"
    send_ntfy(
        body,
        title,
        priority,
        root,
        tags=tags,
        extra_headers={"X-Markdown": "true"},
    )


# ── email notification ────────────────────────────────────────────────────


def _send_email(root: Path, report) -> None:
    """Send a health report via SMTP email."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    cfg = load_config(config_path(root))
    total_issues = sum(
        [
            report.updates.total,
            len(report.containers.unhealthy),
            len(report.containers.restarting),
        ]
    )
    if report.has_issues():
        subject = f"Homelab Health: {total_issues} issue(s) for {cfg.domain}"
    else:
        subject = f"Homelab Health: All clear for {cfg.domain}"
    html_body = report.format_html()
    send_email(subject, "Health report attached. Enable HTML to view full report.", root, html_body=html_body)
