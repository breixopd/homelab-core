"""Controller-owned identity and account resources."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from toolkit.controller.contracts import (
    DeleteDirectoryIdentityCommand,
    ErrorCode,
    IdentityAction,
    IdentityCommand,
    IdentityOperationResult,
    IdentityOutcome,
    IdentityStepResult,
    InviteUserCommand,
    ReprovisionUserCommand,
    ServiceGroupName,
    SetUserGroupsCommand,
)
from toolkit.controller.inventory_api import read_services_view
from toolkit.controller.read_models import (
    AccountView,
    DirectoryGroupView,
    DirectoryUsersView,
    DirectoryUserView,
    InviteActivationRequest,
    InviteActivationResult,
    InvitePreview,
)
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.identity.invite_email import deliver_welcome_email
from toolkit.core.identity.invite_token import (
    TOKEN_MAX_AGE_SECONDS,
    begin_invite_activation,
    complete_invite_activation,
    invite_csrf_token,
    peek_invite_token,
    validate_invite_csrf,
)
from toolkit.core.identity.lldap_client import LLDAPClient
from toolkit.core.identity.service_groups import (
    HOMELAB_GROUP_NAMES,
    HOMELAB_SERVICE_GROUPS,
    default_user_groups_for_enabled_services,
)
from toolkit.core.identity.user_provision import (
    ServiceProvisionReport,
    invite_directory_user,
    provision_user_services,
)
from toolkit.core.secrets.secrets import load_secrets_plaintext
from toolkit.services.sdk import authelia_public_url_for_domain


class InviteRequestRejectedError(RuntimeError):
    pass


class DirectoryUnavailableError(RuntimeError):
    pass


class DirectoryMutationError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


_PROTECTED_DIRECTORY_USERS = frozenset({"admin", "ldap-bind"})


def _secure_invites_configured(cfg: Config, secrets: dict[str, str]) -> bool:
    return bool(
        cfg.category_enabled("email")
        and secrets.get("LLDAP_ADMIN_PASSWORD", "")
        and len(secrets.get("INVITE_TOKEN_SECRET", "")) >= 32
    )


def _directory_user_by_id(client: LLDAPClient, user_id: str):
    return next((user for user in client.list_users() if user.id == user_id), None)


def _verify_managed_groups(client: LLDAPClient, user_id: str, expected: list[ServiceGroupName]) -> None:
    observed = {group for group in client.user_group_names(user_id) if group in HOMELAB_GROUP_NAMES}
    if observed != set(expected):
        raise DirectoryMutationError("OPERATION_FAILED", "Directory group membership did not converge")


def _identity_result(
    *,
    action: IdentityAction,
    user_id: str,
    completed_steps: list[str],
    step_results: list[IdentityStepResult] | None = None,
    provisioning: ServiceProvisionReport | None = None,
) -> dict[str, Any]:
    steps = [IdentityStepResult(key=key, status="completed") for key in completed_steps]
    steps.extend(step_results or [])
    if provisioning is not None:
        steps.extend(IdentityStepResult(key=step.key, status=step.status) for step in provisioning.steps)
    statuses = {step.status for step in steps}
    outcome: IdentityOutcome
    if "failed" in statuses:
        outcome = "partial_failure"
    elif "warning" in statuses:
        outcome = "completed_with_warnings"
    else:
        outcome = "completed"
    return IdentityOperationResult(
        action=action,
        user_id=user_id,
        outcome=outcome,
        steps=steps,
    ).model_dump(mode="json")


def execute_directory_command(
    root: Path,
    command: IdentityCommand,
    *,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    replay: bool = False,
    execution_id: str | None = None,
) -> dict[str, Any]:
    progress = on_progress or (lambda _message, _payload: None)
    cancel = check_cancelled or (lambda: None)
    user_id = getattr(command, "user_id", "")
    if user_id in _PROTECTED_DIRECTORY_USERS:
        raise DirectoryMutationError("FORBIDDEN", "Built-in directory accounts cannot be changed")

    root = root.resolve()
    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin_password:
        raise DirectoryMutationError("OPERATION_REJECTED", "Directory administration is not configured")
    client = LLDAPClient(admin_password=admin_password, root=root)
    cancel()

    if isinstance(command, InviteUserCommand):
        if not _secure_invites_configured(cfg, secrets):
            raise DirectoryMutationError(
                "OPERATION_REJECTED",
                "Secure email activation must be configured before inviting users",
            )
        existing = client.find_user(command.email)
        if existing is not None and existing.id in _PROTECTED_DIRECTORY_USERS:
            raise DirectoryMutationError("FORBIDDEN", "Built-in directory accounts cannot be changed")
        user, _logs = invite_directory_user(
            client,
            command.email,
            display_name=command.display_name or None,
            groups=list(command.groups),
        )
        _verify_managed_groups(client, user.id, list(command.groups))
        progress("Directory account configured", {"action": command.action, "user_id": user.id})
        delivery = deliver_welcome_email(
            cfg,
            secrets,
            email=user.email,
            user_id=user.id,
            display_name=command.display_name or None,
            groups=list(command.groups),
            delivery_id=execution_id,
        )
        if delivery.status != "sent":
            progress(
                "Welcome email delivery failed",
                {"action": command.action, "user_id": user.id, "reason": delivery.reason},
            )
            return _identity_result(
                action=command.action,
                user_id=user.id,
                completed_steps=["directory"],
                step_results=[IdentityStepResult(key="welcome_email", status="failed")],
            )
        progress("Welcome email delivered", {"action": command.action, "user_id": user.id})
        provisioning = provision_user_services(
            cfg,
            secrets,
            user.email,
            list(command.groups),
            notify=False,
            root=root,
        )
        progress(
            "Service provisioning completed",
            {
                "action": command.action,
                "user_id": user.id,
                "outcome": "complete" if provisioning.successful else "partial_failure",
            },
        )
        return _identity_result(
            action=command.action,
            user_id=user.id,
            completed_steps=["directory", "welcome_email"],
            provisioning=provisioning,
        )

    user = _directory_user_by_id(client, user_id)
    if user is None:
        if replay and isinstance(command, DeleteDirectoryIdentityCommand):
            return _identity_result(
                action=command.action,
                user_id=command.user_id,
                completed_steps=["directory_identity"],
            )
        raise DirectoryMutationError("NOT_FOUND", "Directory user was not found")

    if isinstance(command, ReprovisionUserCommand):
        groups = [group for group in client.user_group_names(user.id) if group in HOMELAB_GROUP_NAMES]
        if not _secure_invites_configured(cfg, secrets):
            raise DirectoryMutationError(
                "OPERATION_REJECTED",
                "Secure email activation must be configured before reprovisioning users",
            )
        cancel()
        delivery = deliver_welcome_email(
            cfg,
            secrets,
            email=user.email,
            user_id=user.id,
            display_name=user.display_name or None,
            groups=groups,
            delivery_id=execution_id,
        )
        if delivery.status != "sent":
            progress(
                "Welcome email delivery failed",
                {"action": command.action, "user_id": user.id, "reason": delivery.reason},
            )
            return _identity_result(
                action=command.action,
                user_id=user.id,
                completed_steps=[],
                step_results=[IdentityStepResult(key="welcome_email", status="failed")],
            )
        progress("Welcome email delivered", {"action": command.action, "user_id": user.id})
        provisioning = provision_user_services(cfg, secrets, user.email, groups, notify=True, root=root)
        progress(
            "Service provisioning completed",
            {
                "action": command.action,
                "user_id": user.id,
                "outcome": "complete" if provisioning.successful else "partial_failure",
            },
        )
        return _identity_result(
            action=command.action,
            user_id=user.id,
            completed_steps=["welcome_email"],
            provisioning=provisioning,
        )

    if isinstance(command, SetUserGroupsCommand):
        cancel()
        client.ensure_homelab_groups(list(command.groups))
        client.set_user_groups(user.id, list(command.groups))
        _verify_managed_groups(client, user.id, list(command.groups))
        progress(
            "Directory groups updated",
            {"action": command.action, "user_id": user.id, "groups": list(command.groups)},
        )
        provisioning = provision_user_services(
            cfg,
            secrets,
            user.email,
            list(command.groups),
            notify=False,
            root=root,
        )
        progress(
            "Service provisioning completed",
            {
                "action": command.action,
                "user_id": user.id,
                "outcome": "complete" if provisioning.successful else "partial_failure",
            },
        )
        return _identity_result(
            action=command.action,
            user_id=user.id,
            completed_steps=["directory_groups"],
            provisioning=provisioning,
        )

    if isinstance(command, DeleteDirectoryIdentityCommand):
        cancel()
        client.delete_user(user.id)
        progress("Directory user deleted", {"action": command.action, "user_id": user.id})
        return _identity_result(
            action=command.action,
            user_id=user.id,
            completed_steps=["directory_identity"],
        )

    raise DirectoryMutationError("VALIDATION_ERROR", "Unsupported identity command")


def read_directory_users(root: Path) -> DirectoryUsersView:
    root = root.resolve()
    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
    if not bind_password:
        raise DirectoryUnavailableError("directory read credential is not configured")
    try:
        client = LLDAPClient(admin_password=bind_password, root=root, username="ldap-bind")
        users = client.list_users()
        groups = client.list_groups()
    except Exception:
        raise DirectoryUnavailableError("directory query failed") from None

    memberships: dict[str, list[ServiceGroupName]] = {user.id: [] for user in users}
    managed_groups = set(HOMELAB_GROUP_NAMES)
    for group in groups:
        name = str(group.get("displayName") or "")
        if name not in managed_groups:
            continue
        managed_name = cast(ServiceGroupName, name)
        for member in group.get("users") or []:
            user_id = str(member.get("id") or "")
            if user_id in memberships:
                memberships[user_id].append(managed_name)

    default_groups = set(default_user_groups_for_enabled_services(cfg.services))
    invites_enabled = _secure_invites_configured(cfg, secrets)
    if not cfg.category_enabled("email"):
        disabled_reason = "Email service is required for secure account activation."
    elif not invites_enabled:
        disabled_reason = "Identity invitation credentials are not configured."
    else:
        disabled_reason = ""
    return DirectoryUsersView(
        domain=cfg.domain,
        users=[
            DirectoryUserView(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                groups=sorted(memberships[user.id]),
                is_protected=user.id in _PROTECTED_DIRECTORY_USERS,
            )
            for user in sorted(users, key=lambda item: item.id)
        ],
        group_options=[
            DirectoryGroupView(
                name=cast(ServiceGroupName, group.name),
                label=group.label,
                description=group.description,
                is_default=group.name in default_groups,
            )
            for group in HOMELAB_SERVICE_GROUPS
        ],
        invites_enabled=invites_enabled,
        invite_disabled_reason=disabled_reason,
    )


def read_account_view(root: Path, *, groups: list[str]) -> AccountView:
    services = read_services_view(root, family=True, groups=groups)
    return AccountView(
        domain=services.domain,
        auth_url=authelia_public_url_for_domain(services.domain),
        sections=services.family_sections,
        tier_labels=services.tier_labels,
    )


def _invite_context(root: Path):
    root = root.resolve()
    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    return root, cfg, secrets


def preview_invite(root: Path, token: str) -> InvitePreview:
    root, cfg, secrets = _invite_context(root)
    payload = peek_invite_token(secrets, token) if token else None
    if payload is None:
        return InvitePreview(
            valid=False,
            domain=cfg.domain,
            secure_cookie=cfg.domain != "localhost",
            cookie_max_age_seconds=TOKEN_MAX_AGE_SECONDS,
            sections=[],
        )
    services = read_services_view(root, family=True, groups=payload["groups"])
    return InvitePreview(
        valid=True,
        domain=cfg.domain,
        secure_cookie=cfg.domain != "localhost",
        cookie_max_age_seconds=TOKEN_MAX_AGE_SECONDS,
        activation_csrf=invite_csrf_token(secrets, token),
        display_name=payload["display_name"] or payload["email"],
        email=payload["email"],
        sections=services.family_sections,
    )


def activate_invite(root: Path, request: InviteActivationRequest) -> InviteActivationResult:
    root, cfg, secrets = _invite_context(root)
    secure_cookie = cfg.domain != "localhost"
    scheme = "https" if secure_cookie else "http"
    expected_origin = f"{scheme}://homelab.{cfg.domain}"
    if request.origin != expected_origin or not validate_invite_csrf(
        secrets,
        request.token,
        request.activation_csrf,
    ):
        raise InviteRequestRejectedError("invite activation request was rejected")

    payload = peek_invite_token(secrets, request.token)
    if payload is None:
        return InviteActivationResult(outcome="invalid", secure_cookie=secure_cookie)

    bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
    if not bind_password:
        return InviteActivationResult(outcome="failed", secure_cookie=secure_cookie)
    client = LLDAPClient(admin_password=bind_password, root=root, username="ldap-bind")
    activation_id = ""
    try:
        directory_user = client.find_user(payload["email"])
        if (
            directory_user is None
            or directory_user.id != payload["user_id"]
            or directory_user.email.strip().lower() != payload["email"].strip().lower()
        ):
            return InviteActivationResult(outcome="failed", secure_cookie=secure_cookie)
        activation = begin_invite_activation(secrets, request.token)
        if activation.state == "succeeded":
            return InviteActivationResult(outcome="activated", secure_cookie=secure_cookie)
        if activation.state != "acquired" or activation.payload != payload:
            return InviteActivationResult(outcome="failed", secure_cookie=secure_cookie)
        activation_id = activation.activation_id
        client.set_password(payload["user_id"], request.password)
        if not complete_invite_activation(
            secrets,
            request.token,
            activation_id,
            succeeded=True,
        ):
            return InviteActivationResult(outcome="failed", secure_cookie=secure_cookie)
    except Exception:
        if activation_id:
            complete_invite_activation(
                secrets,
                request.token,
                activation_id,
                succeeded=False,
            )
        return InviteActivationResult(outcome="failed", secure_cookie=secure_cookie)
    return InviteActivationResult(outcome="activated", secure_cookie=secure_cookie)
