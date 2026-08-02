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
import math
import re
import smtplib
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
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
    # SMTPS (implicit TLS), conventionally used on port 465.  STARTTLS is
    # represented separately so callers cannot accidentally downgrade it.
    implicit_tls: bool = False


@dataclass(frozen=True, slots=True)
class SMTPProbeResult:
    """Bounded, no-mail readiness result for an SMTP transport."""

    ok: bool
    stage: str
    detail: str


_SMTP_ERROR_MAX = 180
_SECRET_ASSIGNMENT_RE = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;]+")
_URL_USERINFO_RE = re.compile(r"(?i)(smtps?://)[^/@\s]+@")
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="smtp-dns")
_DNS_SLOTS = threading.BoundedSemaphore(2)


def _sanitize_smtp_error(exc: BaseException, transport: SMTPTransport | None = None) -> str:
    """Return a short diagnostic that cannot expose SMTP credentials."""
    detail = f"{type(exc).__name__}: {exc}"
    if transport is not None:
        for secret in (transport.password,):
            if secret:
                detail = detail.replace(secret, "<redacted>")
    detail = _URL_USERINFO_RE.sub(r"\1<redacted>@", detail)
    detail = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", detail)
    return " ".join(detail.split())[:_SMTP_ERROR_MAX]


def probe_smtp_transport(transport: SMTPTransport, *, timeout: float = 10.0) -> SMTPProbeResult:
    """Probe SMTP readiness without sending a message.

    The probe performs DNS/TCP connection, EHLO, configured TLS negotiation,
    and AUTH when credentials are configured.  TLS uses the system trust
    store; authentication or required-TLS failures are never downgraded.
    ``timeout`` is bounded to prevent a health check from hanging indefinitely.
    """
    try:
        candidate_timeout = float(timeout)
    except (TypeError, ValueError, OverflowError):
        candidate_timeout = 10.0
    bounded_timeout = min(max(candidate_timeout, 0.1), 30.0) if math.isfinite(candidate_timeout) else 10.0
    if not transport.host or not (1 <= transport.port <= 65_535):
        return SMTPProbeResult(False, "config", "SMTP host and port are required")
    if transport.implicit_tls and transport.starttls:
        return SMTPProbeResult(False, "config", "SMTP implicit TLS and STARTTLS cannot both be enabled")
    if bool(transport.username) != bool(transport.password):
        return SMTPProbeResult(False, "auth", "SMTP authentication requires username and password")
    if not _DNS_SLOTS.acquire(timeout=bounded_timeout):
        return SMTPProbeResult(False, "dns", "SMTP DNS resolver is busy")
    release_lock = threading.Lock()
    released = False

    def release_dns_slot() -> None:
        nonlocal released
        with release_lock:
            if not released:
                released = True
                _DNS_SLOTS.release()

    try:
        dns_lookup = _DNS_EXECUTOR.submit(
            socket.getaddrinfo,
            transport.host,
            transport.port,
            type=socket.SOCK_STREAM,
        )
    except RuntimeError as exc:
        release_dns_slot()
        return SMTPProbeResult(False, "dns", _sanitize_smtp_error(exc, transport))
    dns_lookup.add_done_callback(lambda _future: release_dns_slot())
    try:
        addresses = dns_lookup.result(timeout=bounded_timeout)
        if not addresses:
            return SMTPProbeResult(False, "dns", "SMTP host did not resolve")
    except FuturesTimeoutError:
        if dns_lookup.cancel():
            release_dns_slot()
        return SMTPProbeResult(False, "dns", "SMTP DNS lookup timed out")
    except (OSError, UnicodeError, ValueError) as exc:
        return SMTPProbeResult(False, "dns", _sanitize_smtp_error(exc, transport))

    client: smtplib.SMTP | smtplib.SMTP_SSL
    stage = "connect"
    try:
        if transport.implicit_tls:
            client = smtplib.SMTP_SSL(
                transport.host,
                transport.port,
                timeout=bounded_timeout,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(transport.host, transport.port, timeout=bounded_timeout)
        with client as server:
            stage = "ehlo"
            ehlo_code, _ = server.ehlo()
            if not 200 <= ehlo_code < 400:
                return SMTPProbeResult(False, "ehlo", f"SMTP EHLO failed ({ehlo_code})")
            if transport.starttls:
                stage = "tls"
                tls_code, _ = server.starttls(context=ssl.create_default_context())
                if not 200 <= tls_code < 400:
                    return SMTPProbeResult(False, "tls", f"SMTP STARTTLS failed ({tls_code})")
                stage = "ehlo"
                ehlo_code, _ = server.ehlo()
                if not 200 <= ehlo_code < 400:
                    return SMTPProbeResult(False, "ehlo", f"SMTP EHLO after TLS failed ({ehlo_code})")
            if transport.username:
                stage = "auth"
                auth_code, _ = server.login(transport.username, transport.password)
                if not 200 <= auth_code < 400:
                    return SMTPProbeResult(False, "auth", f"SMTP authentication failed ({auth_code})")
            stage = "envelope"
            mail_code, _ = server.mail(transport.from_address)
            if not 200 <= mail_code < 400:
                return SMTPProbeResult(False, "envelope", f"SMTP sender rejected ({mail_code})")
            server.rset()
        return SMTPProbeResult(
            True,
            "ready",
            "SMTP connection, TLS, authentication, and sender envelope verified",
        )
    except (OSError, UnicodeError, ValueError, smtplib.SMTPException, ssl.SSLError) as exc:
        return SMTPProbeResult(False, stage, _sanitize_smtp_error(exc, transport))


def resolve_smtp_transport(cfg: Config, secrets: dict[str, str]) -> SMTPTransport | None:
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
            implicit_tls=policy.port == 465 and not policy.starttls,
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
    transport: SMTPTransport | None = None
    try:
        cfg = load_config(config_path(root))
        transport = resolve_smtp_transport(cfg, _load_secrets(root))
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

        smtp_context = ssl.create_default_context()
        smtp_client: smtplib.SMTP | smtplib.SMTP_SSL
        if transport.implicit_tls:
            smtp_client = smtplib.SMTP_SSL(
                transport.host,
                transport.port,
                timeout=15,
                context=smtp_context,
            )
        else:
            smtp_client = smtplib.SMTP(transport.host, transport.port, timeout=15)
        with smtp_client as server:
            if transport.starttls:
                server.starttls(context=smtp_context)
            if transport.username:
                server.login(transport.username, transport.password)
            server.sendmail(transport.from_address, [to_addr], msg.as_string())
        click.echo(f"  Email notification sent to {to_addr}", err=True)
    except Exception as exc:
        detail = _sanitize_smtp_error(exc, transport)
        click.secho(f"  [warn] email notification failed: {detail}", err=True, fg="yellow")
