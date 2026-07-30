"""LLDAP post-deploy bootstrap: POSIX schema, service-bind account, and owner user/groups."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def sync_ldap_bind_only(root: Path) -> list[str]:
    """Sync the ldap-bind service account before dependent services start."""
    from toolkit.core.config.config import config_path, load_config
    from toolkit.core.identity.lldap_client import LLDAPClient
    from toolkit.core.secrets.secrets import load_runtime_secrets

    cfg = load_config(config_path(root))
    secrets = load_runtime_secrets(root, role=cfg.control_node)
    bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
    admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not bind_password or not admin_password:
        return ["LLDAP: skip ldap-bind sync (missing secrets)"]
    client = LLDAPClient(admin_password=admin_password, root=root)
    return [f"LLDAP: {line}" for line in client.ensure_service_bind(bind_password, domain=cfg.domain or "")]


def bootstrap_lldap_user(config: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
    from toolkit.core.identity.lldap_client import LLDAPClient
    from toolkit.core.identity.service_groups import OWNER_BOOTSTRAP_GROUPS

    logs: list[str] = []
    email = (config.email or "").strip()
    admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
    user_password = secrets.get("SSO_USER_PASSWORD") or admin_password
    if not email:
        logs.append("LLDAP: config.email not set — skip user bootstrap")
        return logs
    if not admin_password:
        logs.append("LLDAP: LLDAP_ADMIN_PASSWORD not set — skip user bootstrap")
        return logs

    try:
        client = LLDAPClient(admin_password=admin_password, root=root)
        # POSIX schema (uidNumber/gidNumber/homeDirectory/unixShell/sshPublicKey +
        # posixAccount objectClass) must exist before any user/group gets POSIX
        # attributes. ensure_service_bind() calls ensure_user_posix() internally,
        # which would otherwise fail with "Attribute gidNumber is not defined in
        # the schema" on a freshly-deployed LLDAP.
        for line in client.ensure_posix_schema():
            logs.append(f"LLDAP: {line}")
        if bind_password:
            for line in client.ensure_service_bind(bind_password, domain=config.domain or ""):
                logs.append(f"LLDAP: {line}")
        else:
            logs.append("LLDAP: LLDAP_BIND_PASSWORD not set — skip service bind account")
            raise RuntimeError("LLDAP_BIND_PASSWORD not set")
        logs.extend(client.ensure_homelab_users_group())
        logs.extend(client.ensure_homelab_group_gids())
        logs.extend(
            client.ensure_owner(
                email,
                user_password,
                domain=config.domain or "",
                groups=list(OWNER_BOOTSTRAP_GROUPS),
                user_id=config.owner_username or None,
            )
        )
        logs.extend(client.ensure_all_users_posix())
        for line in client.ensure_homelab_groups():
            logs.append(f"LLDAP: {line}")
    except RuntimeError as exc:
        logs.append(f"LLDAP: user bootstrap failed ({exc})")
    return logs


def ensure_fleet_user(config: Config, root: Path, email: str) -> list[str]:
    """Create a fleet identity idempotently before SSSD enrollment."""
    import secrets as py_secrets

    from toolkit.core.config.storage import secrets_path
    from toolkit.core.identity.lldap_client import LLDAPClient
    from toolkit.core.identity.service_groups import DEFAULT_NEW_USER_GROUPS
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    normalized = email.strip().lower()
    if not normalized:
        return ["LLDAP: no email provided - skip fleet identity"]
    secrets = load_secrets_plaintext(secrets_path(root))
    admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin_password:
        return ["LLDAP: LLDAP_ADMIN_PASSWORD not set - skip fleet identity"]
    try:
        client = LLDAPClient(admin_password=admin_password, root=root)
        existing = client.find_user(normalized)
        if existing:
            return [f"LLDAP: user {existing.id} already exists"]
        created = client.create_user(normalized)
        client.ensure_groups(created.id, list(DEFAULT_NEW_USER_GROUPS))
        client.set_password(created.id, py_secrets.token_urlsafe(16))
        client.ensure_user_posix(created.id)
        return [f"LLDAP: created fleet user {created.id} ({normalized}) - invite with homelab-toolkit users invite"]
    except RuntimeError as exc:
        return [f"LLDAP: fleet user create failed ({exc})"]
