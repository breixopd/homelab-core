"""Push the ldap-client role to managed machines and fleet nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from toolkit.core.ansible.ansible_runner import run_playbook_sync
from toolkit.core.config.config import config_path, load_config
from toolkit.core.infra.iac_sync import sync_from_repo_root


@dataclass
class LdapSyncResult:
    ok: bool
    message: str
    logs: list[str]


def _playbook_path(root: Path) -> Path:
    return root / "automation" / "ansible" / "playbooks" / "sync-ldap-clients.yml"


def sync_ldap_clients(
    root: Path,
    *,
    limit: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> LdapSyncResult:
    """Render Ansible vars from config and apply ldap-client on guests/fleet."""
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    playbook = _playbook_path(root)
    if not playbook.is_file():
        return LdapSyncResult(False, "sync-ldap-clients.yml missing", logs)

    cfg = load_config(config_path(root))
    from toolkit.core.infra.fleet import list_nodes

    if not cfg.is_multi_node and not list_nodes(root):
        return LdapSyncResult(True, "single-host mode — no remote ldap-client sync needed", logs)

    sync_from_repo_root(root)
    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    if not inventory.is_file():
        from toolkit.core.ansible.ansible_inventory import write_inventory

        write_inventory(root, cfg)

    log(f"Running ldap-client sync{f' (limit {limit})' if limit else ''}…")
    result = run_playbook_sync(
        root,
        playbook,
        inventory=inventory,
        limit=limit,
        on_log=log,
    )
    if not result.ok:
        return LdapSyncResult(
            False,
            f"ldap-client sync failed (exit {result.returncode})",
            logs,
        )
    return LdapSyncResult(True, "ldap-client synced on target hosts", logs)
