"""Shared best-effort notification helpers (ntfy + email) for CLI commands.

Both ``toolkit.cli.update`` and ``toolkit.cli.health`` need to push a
notification through ntfy and/or SMTP. The plumbing (config/secrets loading,
ntfy URL resolution, SMTP wiring, swallow-and-warn error handling) is
identical; only the message body differs. This module owns the plumbing so
the CLI commands only build their message and call ``send_ntfy``/``send_email``.

All public functions are best-effort: they log a warning on failure and never
raise, so a misconfigured ntfy URL or SMTP server never breaks the parent
command.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import click

from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.deploy.deploy_notify import resolve_deploy_notify_url
from toolkit.services.ntfy.client import normalize_ntfy_url, post_ntfy_url

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SMTPTransport:
    host: str
    port: int
    from_address: str
    starttls: bool = False
    username: str = ""
    password: str = ""


def _resolve_smtp_transport(cfg: Config, secrets: dict[str, str]) -> SMTPTransport | None:
    policy = cfg.notifications.smtp
    from_address = policy.from_address or f"noreply@{cfg.domain}"
    if policy.mode == "disabled":
        return None
    if policy.mode == "external":
        password = secrets.get(policy.password_secret, "") if policy.password_secret else ""
        if policy.username and not password:
            raise ValueError(f"SMTP password secret {policy.password_secret!r} is not configured")
        return SMTPTransport(
            host=policy.host,
            port=policy.port,
            from_address=from_address,
            starttls=policy.starttls,
            username=policy.username,
            password=password,
        )

    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_address
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    mailserver = catalog.require("mailserver")
    if not service_is_enabled(cfg, mailserver, catalog):
        return None
    endpoint = mailserver.service_endpoint
    if endpoint is None:
        raise ValueError("mailserver does not declare a service endpoint")
    return SMTPTransport(
        host=service_address(cfg, mailserver.name),
        port=endpoint.published_port or endpoint.container_port,
        from_address=from_address,
    )


def _load_secrets(root: Path) -> dict[str, str]:
    """Load decrypted secrets for the given homelab root (empty dict on failure)."""
    sp = secrets_path(root)
    if not sp.exists():
        return {}
    try:
        from toolkit.core.secrets.secrets import load_secrets_plaintext

        return load_secrets_plaintext(sp)
    except Exception as exc:
        log.warning("Failed to load secrets for notifications: %s", exc)
        return {}


def send_ntfy(
    message: str,
    title: str,
    priority: str,
    root: Path,
    *,
    tags: str = "",
    extra_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> None:
    """POST ``message`` to the configured ntfy topic. Best-effort, never raises.

    The ntfy URL is resolved from secrets (canonical source) first, then from
    ``notifications.deploy_ntfy_url`` in config. If no URL is configured, the
    call is silently skipped with an info message.
    """
    try:
        cfg = load_config(config_path(root))
        secrets = _load_secrets(root)
        raw = resolve_deploy_notify_url(cfg, secrets)
        if not raw:
            click.echo("  [info] ntfy notification skipped: no ntfy URL configured", err=True)
            return
        post_url = normalize_ntfy_url(raw)
        if not post_url:
            return
        post_ntfy_url(
            post_url,
            message,
            title=title,
            priority=priority,
            tags=tags,
            extra_headers=extra_headers,
            timeout=timeout,
        )
        click.echo("  ntfy notification sent", err=True)
    except Exception as exc:
        click.secho(f"  [warn] ntfy notification skipped: {exc}", err=True, fg="yellow")


def send_email(
    subject: str,
    body: str,
    root: Path,
    *,
    html_body: str | None = None,
) -> None:
    """Send an email via SMTP. Best-effort, never raises.

    Transport policy comes from ``notifications.smtp``. Auto mode uses the
    manifest-declared mailserver endpoint; external mode uses encrypted
    credentials. The recipient is read from ``config.email``.
    """
    try:
        cfg = load_config(config_path(root))
        transport = _resolve_smtp_transport(cfg, _load_secrets(root))
        if transport is None:
            click.echo("  [info] email notification skipped: SMTP transport disabled", err=True)
            return

        to_addr = getattr(cfg, "email", "")
        if not to_addr:
            click.echo(
                "  [info] email notification skipped: no email configured in config.yaml",
                err=True,
            )
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = transport.from_address
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(transport.host, transport.port, timeout=15) as server:
            if transport.starttls:
                server.starttls()
            if transport.username:
                server.login(transport.username, transport.password)
            server.sendmail(transport.from_address, [to_addr], msg.as_string())
        click.echo(f"  Email notification sent to {to_addr}", err=True)
    except Exception as exc:
        click.secho(f"  [warn] email notification failed: {exc}", err=True, fg="yellow")
