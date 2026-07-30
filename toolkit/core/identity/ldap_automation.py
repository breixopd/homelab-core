"""Automated LLDAP POSIX + SSSD sync — no manual ldap/sssd steps."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from toolkit.core.config.config import Config, config_path, load_config
from toolkit.core.config.storage import secrets_path
from toolkit.core.identity.ldap_guest_sync import sync_ldap_clients
from toolkit.core.identity.lldap_client import LLDAPClient
from toolkit.core.secrets.secrets import load_secrets_plaintext

_GUEST_ROOT = os.environ.get("HOMELAB_GUEST_ROOT", "/opt/homelab")


def _controller_remote(root: Path) -> bool:
    cfg = load_config(config_path(root))
    return os.environ.get("HOMELAB_DEPLOY_CONTROLLER") == "1" and cfg.is_multi_node


def _run_on_infra(root: Path, python_snippet: str, *, extra_env: dict[str, str] | None = None) -> list[str]:
    """Execute a short Python snippet on the node hosting LLDAP."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address, service_node

    cfg = load_config(config_path(root))
    directory_node = service_node(cfg, "lldap")
    directory_ip = service_address(cfg, "lldap")
    guest = _GUEST_ROOT
    env_parts = [f"PYTHONPATH={guest}", f"HOMELAB_NODE={directory_node}"]
    for key, value in (extra_env or {}).items():
        env_parts.append(f"{key}={shlex.quote(value)}")
    env = " ".join(env_parts)
    cmd = (
        f"cd {shlex.quote(guest)} && {env} {shlex.quote(guest + '/.venv/bin/python')} -c {shlex.quote(python_snippet)}"
    )
    rc, out, err = ssh_run_on_vm(cfg, directory_ip, cmd, root=root, timeout=180)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if rc != 0:
        lines.append(f"LDAP remote failed (exit {rc}): {(err or out or '')[:200]}")
    return lines


def _repair_posix_with_client(client: LLDAPClient) -> list[str]:
    logs: list[str] = []
    try:
        logs.extend(client.ensure_posix_schema())
        logs.extend(client.ensure_homelab_users_group())
        logs.extend(client.ensure_homelab_group_gids())
        logs.extend(client.ensure_all_users_posix())
    except RuntimeError as exc:
        logs.append(f"LDAP: POSIX repair failed ({exc})")
    return logs


def repair_directory_posix(root: Path) -> list[str]:
    """Ensure LLDAP schema + POSIX attrs for all users (idempotent)."""
    if _controller_remote(root):
        secrets = load_secrets_plaintext(secrets_path(root))
        admin = secrets.get("LLDAP_ADMIN_PASSWORD", "")
        if not admin:
            return ["LDAP: skip POSIX repair (LLDAP_ADMIN_PASSWORD missing)"]
        snippet = (
            "import os; from pathlib import Path; "
            "from toolkit.core.identity.ldap_automation import _repair_posix_local; "
            f"root = Path(os.environ.get('HOMELAB_ROOT', '{_GUEST_ROOT}')); "
            "[print(x) for x in _repair_posix_local(root)]"
        )
        return _run_on_infra(root, snippet, extra_env={"LLDAP_ADMIN_PASSWORD": admin})
    return _repair_posix_local(root)


def _repair_posix_local(root: Path) -> list[str]:
    admin = os.environ.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin:
        secrets = load_secrets_plaintext(secrets_path(root))
        admin = secrets.get("LLDAP_ADMIN_PASSWORD", "")
    if not admin:
        return ["LDAP: skip POSIX repair (LLDAP_ADMIN_PASSWORD missing)"]
    client = LLDAPClient(admin_password=admin, root=root)
    return _repair_posix_with_client(client)


def sync_sssd_guests(root: Path, *, limit: str | None = None) -> list[str]:
    """Push the ldap-client role to managed machines and fleet nodes."""
    from toolkit.core.infra.fleet import list_nodes

    cfg = load_config(config_path(root))
    if not cfg.is_multi_node and not list_nodes(root):
        return ["LDAP: single-host — SSSD sync not needed"]
    result = sync_ldap_clients(root, limit=limit)
    return result.logs if result.logs else [result.message]


def ensure_directory_and_sssd(root: Path, *, limit: str | None = None, repair: bool = True) -> list[str]:
    """POSIX repair (optional) + SSSD sync — used after deploy hooks and fleet onboard."""
    logs: list[str] = []
    if repair:
        logs.extend(repair_directory_posix(root))
    logs.extend(sync_sssd_guests(root, limit=limit))
    return logs


def sync_sssd_after_hooks(root: Path, cfg: Config) -> list[str]:
    """Post-deploy: refresh SSSD on all guests when multi-VM."""
    if not cfg.is_multi_node:
        return []
    return ensure_directory_and_sssd(root, repair=False)
