"""Render and send unified welcome invite emails."""

from __future__ import annotations

import hashlib
import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from toolkit.core.identity.invite_token import create_invite_token
from toolkit.core.identity.service_groups import invite_sections_for_groups

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

log = logging.getLogger(__name__)
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"


@dataclass(frozen=True)
class WelcomeDelivery:
    status: Literal["sent", "disabled", "failed"]
    reason: Literal["sent", "email_disabled", "template", "smtp"]


def homelab_ui_base_url(config: Config) -> str:
    proto = "https" if config.domain != "localhost" else "http"
    return f"{proto}://homelab.{config.domain}"


def invite_activate_url(config: Config, token: str) -> str:
    return f"{homelab_ui_base_url(config)}/invite/activate?token={token}"


def _render_template(name: str, **context: object) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(name).render(**context)


def build_welcome_email_context(
    config: Config,
    secrets: dict[str, str],
    *,
    email: str,
    user_id: str,
    display_name: str | None,
    groups: list[str],
    delivery_id: str | None = None,
) -> dict[str, object]:
    token = create_invite_token(
        secrets,
        email=email,
        user_id=user_id,
        display_name=display_name,
        groups=groups,
        issuance_id=delivery_id,
    )
    activate_url = invite_activate_url(config, token)
    sections = invite_sections_for_groups(config, groups)
    return {
        "domain": config.domain,
        "email": email,
        "display_name": display_name or email.split("@")[0],
        "activate_url": activate_url,
        "sections": sections,
        "owner_email": config.email,
    }


def deliver_welcome_email(
    config: Config,
    secrets: dict[str, str],
    *,
    email: str,
    user_id: str,
    display_name: str | None,
    groups: list[str],
    delivery_id: str | None = None,
) -> WelcomeDelivery:
    """Send one welcome email and return a stable result without exception details."""
    if not config.category_enabled("email"):
        return WelcomeDelivery(status="disabled", reason="email_disabled")
    try:
        ctx = build_welcome_email_context(
            config,
            secrets,
            email=email,
            user_id=user_id,
            display_name=display_name,
            groups=groups,
            delivery_id=delivery_id,
        )
        html_body = _render_template("invite_email.html.j2", **ctx)
        text_body = _render_template("invite_email.txt.j2", **ctx)
    except Exception:
        log.warning("Welcome email template rendering failed")
        return WelcomeDelivery(status="failed", reason="template")

    from_addr = f"homelab@{config.domain}"
    subject = f"Welcome to {config.domain} — your homelab access"
    try:
        from toolkit.core.manifest.placement import service_address

        smtp_host = service_address(config, "mailserver") if config.is_multi_node else "mailserver"
        boundary = None
        if delivery_id:
            boundary = f"homelab-{hashlib.sha256(delivery_id.encode('utf-8')).hexdigest()[:32]}"
        msg = MIMEMultipart("alternative", boundary=boundary)
        msg["Subject"] = subject
        msg["From"] = f"Homelab <{from_addr}>"
        msg["To"] = email
        if delivery_id:
            message_id = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()[:32]
            msg["Message-ID"] = f"<{message_id}@{config.domain}>"
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(smtp_host, 25, timeout=20) as server:
            server.sendmail(from_addr, [email], msg.as_string())
        return WelcomeDelivery(status="sent", reason="sent")
    except OSError:
        log.warning("Welcome email SMTP delivery failed")
        return WelcomeDelivery(status="failed", reason="smtp")


def send_welcome_email(
    config: Config,
    secrets: dict[str, str],
    *,
    email: str,
    user_id: str,
    display_name: str | None,
    groups: list[str],
    delivery_id: str | None = None,
) -> list[str]:
    """Render the typed delivery result for CLI callers."""
    result = deliver_welcome_email(
        config,
        secrets,
        email=email,
        user_id=user_id,
        display_name=display_name,
        groups=groups,
        delivery_id=delivery_id,
    )
    if result.status == "sent":
        return [f"Welcome email sent to {email}"]
    if result.status == "disabled":
        return ["Welcome email: mail stack disabled"]
    return [f"Welcome email failed ({result.reason})"]
