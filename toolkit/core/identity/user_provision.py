"""Provision LLDAP users into homelab services (invites, OIDC/LDAP onboarding)."""

from __future__ import annotations

import logging
import secrets as py_secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from toolkit.core.identity.invite_email import send_welcome_email
from toolkit.core.identity.lldap_client import LLDAPUser
from toolkit.core.identity.service_groups import (
    DEFAULT_NEW_USER_GROUPS,
    service_urls_for_groups,
    validate_service_groups,
)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.identity.lldap_client import LLDAPClient
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.services import ServicePlugin

log = logging.getLogger(__name__)

ProvisionStepStatus = Literal["completed", "pending", "skipped", "warning", "failed"]


@dataclass(frozen=True, slots=True)
class ServiceProvisionStep:
    """One stable, machine-readable service provisioning outcome."""

    key: str
    status: ProvisionStepStatus
    message: str


@dataclass(frozen=True, slots=True)
class ServiceProvisionReport:
    """Structured outcomes with messages suitable for interactive callers."""

    steps: tuple[ServiceProvisionStep, ...]

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(step.message for step in self.steps)

    @property
    def successful(self) -> bool:
        return all(step.status != "failed" for step in self.steps)


def authelia_portal_url(config: Config) -> str:
    proto = "https" if config.domain != "localhost" else "http"
    return f"{proto}://auth.{config.domain}"


def invite_directory_user(
    client: LLDAPClient,
    email: str,
    *,
    display_name: str | None = None,
    groups: list[str] | None = None,
) -> tuple[LLDAPUser, list[str]]:
    """Create LLDAP user with unknown placeholder password; user sets via Authelia reset."""
    logs: list[str] = []
    email = email.strip().lower()
    client.ensure_homelab_groups()
    existing = client.find_user(email)
    if existing:
        user = existing
        logs.append(f"LLDAP: user {user.id} already exists")
        logs.append("LLDAP: existing password kept until invite activation")
    else:
        user = client.create_user(email, display_name=display_name or None)
        logs.append(f"LLDAP: created user {user.id}")
        placeholder = py_secrets.token_urlsafe(32)
        client.set_password(user.id, placeholder)
        logs.append("LLDAP: placeholder password set (user chooses their own via activation)")

    selected = validate_service_groups(groups or DEFAULT_NEW_USER_GROUPS)
    client.set_user_groups(user.id, selected)
    client.ensure_groups(user.id, [g for g in selected if g.startswith("lldap_")])
    client.ensure_user_posix(user.id)
    logs.append(f"LLDAP: groups {', '.join(g for g in selected if g.startswith('homelab-'))}")
    return user, logs


def send_invite_email(
    config: Config,
    secrets: dict[str, str],
    email: str,
    groups: list[str],
    *,
    user_id: str,
    display_name: str | None = None,
) -> list[str]:
    """Send unified welcome email (single message to invitee)."""
    return send_welcome_email(
        config,
        secrets,
        email=email,
        user_id=user_id,
        display_name=display_name,
        groups=groups,
    )


def invite_and_provision_user(
    config: Config,
    secrets: dict[str, str],
    client: LLDAPClient,
    email: str,
    *,
    display_name: str | None = None,
    groups: list[str] | None = None,
    password: str | None = None,
    notify: bool = True,
    root=None,
) -> list[str]:
    """Full invite: LLDAP account, optional admin-set password, service provisioning, email."""
    if not password and not config.category_enabled("email"):
        raise RuntimeError("Email service must be enabled for password activation invites")
    if password:
        client.ensure_homelab_groups()
        existing = client.find_user(email)
        user = existing or client.create_user(email, display_name=display_name or None)
        client.set_password(user.id, password)
        selected = validate_service_groups(groups or DEFAULT_NEW_USER_GROUPS)
        client.set_user_groups(user.id, selected)
        client.ensure_groups(user.id, [g for g in selected if g.startswith("lldap_")])
        logs = [f"LLDAP: password set by admin for {user.id}"]
    else:
        user, logs = invite_directory_user(client, email, display_name=display_name, groups=groups)
        selected = client.user_group_names(user.id)
        logs.extend(
            send_invite_email(
                config,
                secrets,
                user.email,
                selected,
                user_id=user.id,
                display_name=display_name,
            )
        )

    report = provision_user_services(config, secrets, user.email, selected, notify=notify, root=root)
    logs.extend(report.messages)
    return logs


def provision_user_services(
    config: Config,
    secrets: dict[str, str],
    email: str,
    groups: list[str],
    *,
    notify: bool = True,
    root: Path | None = None,
    catalog: ServiceCatalog | None = None,
    plugins: Mapping[str, ServicePlugin] | None = None,
) -> ServiceProvisionReport:
    """Dispatch plugin-owned provisioning for services matching the selected groups."""
    from toolkit.core.identity.service_groups import effective_access_groups
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.services import IdentityProvisionResult, get_service_plugin

    selected_groups = set(validate_service_groups(groups))
    ordered: list[tuple[int, str, ServiceProvisionStep]] = []
    selected_catalog = catalog or load_service_catalog()
    for manifest in selected_catalog.manifests:
        declarations = manifest.identity.provisioning
        if not declarations or selected_groups.isdisjoint(effective_access_groups(manifest)):
            continue
        if not service_is_enabled(config, manifest, selected_catalog):
            ordered.extend(
                (
                    declaration.priority,
                    manifest.name,
                    ServiceProvisionStep(declaration.id, "skipped", declaration.disabled_message),
                )
                for declaration in declarations
            )
            continue

        ordered.extend(
            (
                declaration.priority,
                manifest.name,
                ServiceProvisionStep(declaration.id, "pending", declaration.message),
            )
            for declaration in declarations
            if declaration.mode == "first_login"
        )
        plugin_declarations = {entry.id: entry for entry in declarations if entry.mode == "plugin"}
        if not plugin_declarations:
            continue
        plugin = plugins.get(manifest.name) if plugins is not None else get_service_plugin(manifest.name)
        results: tuple[IdentityProvisionResult, ...]
        if plugin is None:
            results = ()
        else:
            try:
                results = plugin.provision_identity(
                    config,
                    secrets,
                    email.strip().lower(),
                    root=root,
                )
            except Exception:
                log.exception("identity provisioning failed for %s", manifest.name)
                results = ()
        result_keys = [result.key for result in results]
        if len(result_keys) != len(set(result_keys)) or set(result_keys) - set(plugin_declarations):
            log.error("identity provisioning returned invalid step IDs for %s", manifest.name)
            results = ()
        by_key = {result.key: result for result in results}
        for key, declaration in plugin_declarations.items():
            result = by_key.get(key)
            step = (
                ServiceProvisionStep(result.key, result.status, result.message)
                if result is not None
                else ServiceProvisionStep(key, "failed", f"{manifest.label}: provisioning did not return {key}")
            )
            ordered.append((declaration.priority, manifest.name, step))

    steps = [entry[2] for entry in sorted(ordered, key=lambda item: (item[0], item[1], item[2].key))]

    if notify:
        steps.extend(_notify_service_invite(config, secrets, email, groups, root=root).steps)
    else:
        steps.append(ServiceProvisionStep("owner_notification", "skipped", "Invite: owner notification disabled"))
    return ServiceProvisionReport(steps=tuple(steps))


def _notify_service_invite(
    config: Config,
    secrets: dict[str, str],
    email: str,
    groups: list[str],
    *,
    root=None,
) -> ServiceProvisionReport:
    """Best-effort ntfy/email-style invite with service URLs (owner topic)."""
    urls = service_urls_for_groups(config, groups)
    if not urls:
        return ServiceProvisionReport(
            (ServiceProvisionStep("owner_notification", "skipped", "Invite: no service URLs to notify"),)
        )
    lines = [
        f"Homelab access provisioned for {email}",
        "",
        "The user receives one welcome email with an activation link and app guide.",
        "",
        "Services:",
    ]
    for label, url in urls:
        lines.append(f"  • {label}: {url}")
    message = "\n".join(lines)
    topic = secrets.get("DEPLOY_NTFY_URL", "") or secrets.get("NTFY_TOPIC", "")
    if not topic:
        return ServiceProvisionReport(
            (
                ServiceProvisionStep(
                    "owner_notification",
                    "skipped",
                    "Invite: service URLs available (no ntfy topic configured)",
                ),
            )
        )
    try:
        from toolkit.services.ntfy.client import post_ntfy_url

        if post_ntfy_url(topic, message, title=f"Homelab invite: {email}"):
            step = ServiceProvisionStep("owner_notification", "completed", f"Invite: notification sent for {email}")
        else:
            step = ServiceProvisionStep("owner_notification", "warning", "Invite: ntfy notification failed")
    except Exception:
        step = ServiceProvisionStep("owner_notification", "warning", "Invite: ntfy notification failed")
    return ServiceProvisionReport((step,))


def provision_all_directory_users(config: Config, secrets: dict[str, str], *, root=None) -> list[str]:
    """Re-run service invites for every LLDAP user (maintenance / after enabling cloud)."""
    from toolkit.core.identity.lldap_client import LLDAPClient

    logs: list[str] = []
    admin = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin:
        return ["LLDAP: admin password missing — skip bulk provision"]
    client = LLDAPClient(admin_password=admin, root=root)
    client.ensure_homelab_groups()
    for user in client.list_users():
        if user.id in ("admin", "ldap-bind"):
            continue
        groups = client.user_group_names(user.id)
        if not any(g.startswith("homelab-") for g in groups):
            continue
        logs.append(f"--- {user.email} ---")
        report = provision_user_services(
            config,
            secrets,
            user.email,
            groups,
            notify=False,
            root=root,
        )
        logs.extend(report.messages)
    return logs
