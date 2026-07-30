"""Homelab credential catalog — maps generated secrets to login URLs and Vaultwarden tags."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass(frozen=True)
class CredentialEntry:
    name: str
    secret_key: str
    username_key: str | None
    url_template: str
    tags: tuple[str, ...]
    username: str | None = None
    notes: str = ""
    category: str = "infrastructure"
    is_secure_note: bool = False


def _ldap_ssh_note(config: Config) -> str:
    """Connection notes for human SSH — LDAP password, not automation private keys."""
    domain = config.domain
    uid = (config.email or f"admin@{domain}").split("@", 1)[0]
    lines = [
        "Human SSH uses your LLDAP user + Authelia SSO password (not keys in this vault).",
    ]
    for node in config.enabled_nodes:
        lines.append(f"  ssh {uid}@{config.node_ip(node)}   # {node}")
    lines.append("Automation SSH private keys stay in controller ssh/ and are never synced here.")
    return "\n".join(lines)


def resolve_catalog_value(
    entry: CredentialEntry,
    secrets: dict[str, str],
    root: Path,
    config: Config,
) -> str:
    """Resolve the value stored in Vaultwarden for a catalog entry."""
    if entry.is_secure_note:
        if entry.secret_key:
            val = secrets.get(entry.secret_key, "")
            if val:
                return val
        if entry.name == "Homelab SSH Access":
            return _ldap_ssh_note(config)
        return entry.notes

    if entry.secret_key == "HOMELAB_SSH_PUBLIC_KEY":
        val = secrets.get(entry.secret_key, "")
        if not val:
            pub_file = root / "ssh" / "homelab_admin_ed25519.pub"
            if pub_file.is_file():
                val = pub_file.read_text().strip()
        if not val:
            val = getattr(config.proxmox, "ssh_public_key", "") or ""
        if val:
            return f"{entry.notes}\n\n{val}".strip() if entry.notes else val
        return ""

    if entry.secret_key:
        return secrets.get(entry.secret_key, "")
    return entry.notes


def credential_entries(config: Config) -> list[CredentialEntry]:
    """Return manifest-owned service credentials plus infrastructure access."""
    domain = config.domain
    from toolkit.services import enabled_service_plugins

    entries = [entry for _category, plugin in enabled_service_plugins(config) for entry in plugin.credentials(config)]

    # SSH: pubkey + LDAP connection notes only — no private keys in the catalog.
    control = config.control_node
    control_address = config.node_ip(control)
    entries.extend(
        [
            CredentialEntry(
                "Homelab SSH Access",
                "",
                None,
                f"ssh://ldap-user@{control_address}",
                ("homelab", "homelab/infrastructure", "homelab/ssh"),
                is_secure_note=True,
            ),
            CredentialEntry(
                "Homelab SSH Public Key",
                "HOMELAB_SSH_PUBLIC_KEY",
                None,
                f"ssh://{config.machines[control].effective_ssh_user}@{control_address}:{config.machines[control].ssh_port}",
                ("homelab", "homelab/infrastructure", "homelab/ssh"),
                notes="Automation public key (private key stays in SOPS/controller ssh/)",
                is_secure_note=True,
            ),
            CredentialEntry(
                "Proxmox API Token",
                "PROXMOX_API_TOKEN_ID",
                None,
                f"https://{config.proxmox.node.lower()}.{domain}:8006",
                ("homelab", "homelab/infrastructure", "homelab/proxmox"),
                notes="API token for Terraform/Ansible automation. Secret: PROXMOX_API_TOKEN_SECRET",
            ),
        ]
    )

    return entries


def resolve_username(entry: CredentialEntry, secrets: dict[str, str], config: Config) -> str:
    if entry.username_key:
        val = secrets.get(entry.username_key, "")
        if val:
            return val
    if entry.username:
        return entry.username
    return config.email or f"admin@{config.domain}"
