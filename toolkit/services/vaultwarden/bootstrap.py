"""Push homelab credentials to Vaultwarden (Bitwarden-compatible API) with client-side encryption."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from toolkit.core.config.credential_catalog import credential_entries, resolve_catalog_value, resolve_username
from toolkit.core.secrets.bitwarden_crypto import (
    decrypt_cipher_string,
    encode_account_keys,
    encrypt_cipher_string,
    register_payload,
    unlock_account_keys,
)
from toolkit.services.sdk import (
    BITWARDEN_CLIENT_VERSION,
    vaultwarden_admin_session,
    vaultwarden_fetch_kdf,
    vaultwarden_login_access_token,
    vaultwarden_url,
)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

log = logging.getLogger(__name__)


def _resolve_credential_value(entry, secrets: dict[str, str], root: Path, config: Config | None = None) -> str:
    """Resolve catalog value for sync (delegates to credential_catalog)."""
    if config is None:
        return ""
    return resolve_catalog_value(entry, secrets, root, config)


def _expected_catalog_count(config: Config, secrets: dict[str, str], root: Path) -> int:
    return sum(1 for entry in credential_entries(config) if _resolve_credential_value(entry, secrets, root, config))


def _identity_base(base: str) -> str:
    return f"{base.rstrip('/')}/identity"


def _persist_bitwarden_keys(root: Path, enc_key: bytes, mac_key: bytes) -> None:
    try:
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

        sp = secrets_path(root)
        if not sp.exists():
            return
        enc_b64, mac_b64 = encode_account_keys(enc_key, mac_key)
        stored = load_secrets_plaintext(sp)
        changed = False
        if stored.get("BITWARDEN_ENC_KEY") != enc_b64:
            stored["BITWARDEN_ENC_KEY"] = enc_b64
            changed = True
        if stored.get("BITWARDEN_MAC_KEY") != mac_b64:
            stored["BITWARDEN_MAC_KEY"] = mac_b64
            changed = True
        if changed:
            save_secrets_plaintext(stored, sp)
    except OSError as exc:
        log.warning("Could not persist Bitwarden account keys: %s", exc)


def _hash_admin_token_if_needed(secrets: dict[str, str], root: Path) -> None:
    """No-op: admin token stays plaintext in SOPS; generate hashes for compose .env only."""
    _ = secrets, root


def ensure_vaultwarden_account(
    config: Config,
    secrets: dict[str, str],
    root: Path | None = None,
    *,
    base_url: str | None = None,
) -> list[str]:
    """Admin-invite + register so API login works without manual OIDC signup."""
    logs: list[str] = []
    email = (config.email or f"admin@{config.domain}").strip().lower()
    master = secrets.get("VAULTWARDEN_MASTER_PASSWORD", "")
    if not master:
        logs.append("Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip account bootstrap")
        return logs

    root = root or Path.cwd()
    _hash_admin_token_if_needed(secrets, root)

    base = (base_url or vaultwarden_url(config)).rstrip("/")
    vault_healthy = False
    for attempt in range(1, 4):
        try:
            if httpx.get(f"{base}/alive", timeout=10).status_code == 200:
                vault_healthy = True
                break
        except httpx.HTTPError as exc:
            log.warning("Vaultwarden health check failed: %s", exc)
        if attempt < 3:
            time.sleep(5)
            logs.append(f"Vaultwarden: health check attempt {attempt} failed, retrying in 5s...")
    if not vault_healthy:
        logs.append("Vaultwarden: not reachable after 3 attempts — skip account bootstrap")
        return logs

    kdf = vaultwarden_fetch_kdf(base, email)
    try:
        token = vaultwarden_login_access_token(base, email, master, kdf=kdf)
    except httpx.HTTPError as exc:
        logs.append(f"Vaultwarden: login check failed ({exc})")
        token = ""
    if token:
        logs.append("Vaultwarden: vault account ready (password login)")
        return logs

    admin_token = secrets.get("VAULTWARDEN_ADMIN_TOKEN", "")
    cookies = vaultwarden_admin_session(base, admin_token)
    if cookies is None:
        logs.append("Vaultwarden: no admin session — deploy with VAULTWARDEN_ADMIN_TOKEN in compose")
        return logs

    try:
        httpx.post(
            f"{base}/admin/invite",
            cookies=cookies,
            json={"email": email},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        log.warning("Vaultwarden admin invite failed: %s", exc)

    payload = register_payload(master, email)
    try:
        reg = httpx.post(f"{_identity_base(base)}/accounts/register", json=payload, timeout=30)
        if reg.status_code == 200:
            logs.append("Vaultwarden: registered vault account for automation (Argon2id)")
        elif reg.status_code in (400, 422) and "already" in (reg.text or "").lower():
            logs.append("Vaultwarden: account already exists")
            if not vaultwarden_login_access_token(base, email, master, kdf=vaultwarden_fetch_kdf(base, email)):
                logs.append(
                    "Vaultwarden: configured master password does not unlock the existing account; "
                    "vault preserved — update the configured password or reset the account manually"
                )
                return logs
        else:
            logs.append(f"Vaultwarden: register returned {reg.status_code}")
    except httpx.HTTPError as exc:
        logs.append(f"Vaultwarden: register failed ({exc})")

    try:
        verified = vaultwarden_login_access_token(base, email, master, kdf=vaultwarden_fetch_kdf(base, email))
    except httpx.HTTPError as exc:
        logs.append(f"Vaultwarden: password login pending ({exc})")
        return logs
    if verified:
        logs.append("Vaultwarden: password login verified after bootstrap")
    else:
        logs.append("Vaultwarden: password login pending — check ADMIN_TOKEN and SIGNUPS_DOMAINS_WHITELIST in compose")
    return logs


def _encrypt_field(value: str, enc_key: bytes, mac_key: bytes) -> str:
    return encrypt_cipher_string(value, enc_key, mac_key)


def _build_encrypted_cipher(
    entry,
    value: str,
    username: str,
    enc_key: bytes,
    mac_key: bytes,
) -> dict[str, Any]:
    def enc(value: str) -> str:
        return _encrypt_field(value, enc_key, mac_key)

    if entry.is_secure_note:
        return {
            "type": 2,
            "name": enc(entry.name),
            "notes": enc(value or entry.notes or ""),
            "secureNote": {"type": 0},
        }
    return {
        "type": 1,
        "name": enc(entry.name),
        "notes": enc(entry.notes or ""),
        "login": {
            "username": enc(username),
            "password": enc(value),
            "uris": [{"match": None, "uri": enc(entry.url_template)}],
        },
    }


def _index_personal_ciphers_by_name(
    ciphers: list[dict[str, Any]],
    enc_key: bytes,
    mac_key: bytes,
) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for cipher in ciphers:
        if cipher.get("organizationId"):
            continue
        cipher_id = cipher.get("id")
        name_field = cipher.get("name")
        if not cipher_id or not name_field:
            continue
        try:
            name = decrypt_cipher_string(name_field, enc_key, mac_key).decode("utf-8")
        except (ValueError, subprocess.CalledProcessError, UnicodeDecodeError):
            continue
        by_name[name] = cipher_id
    return by_name


def sync_catalog_to_vaultwarden(
    root: Path,
    config: Config,
    secrets: dict[str, str],
) -> list[str]:
    """Sync credential catalog into the owner's personal vault with client-side encryption."""
    if not secrets.get("VAULTWARDEN_MASTER_PASSWORD"):
        return ["Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip sync"]
    # The production controller is placed on a managed node and therefore has
    # HOMELAB_NODE set, but it is still the control-plane process and must use
    # the controller-to-service tunnel.  HOMELAB_NODE alone only identifies a
    # guest-local hook when no controller role is present.
    controller_role = os.environ.get("HOMELAB_CONTROLLER_ROLE", "").strip().lower()
    if controller_role == "local":
        if config.is_multi_node:
            return _sync_catalog_via_tunnel(root, config, secrets)
        return _sync_catalog_local(root, config, secrets)
    current_node = os.environ.get("HOMELAB_NODE", "")
    if current_node:
        from toolkit.core.manifest.placement import service_address

        machine = config.machines.get(current_node)
        if machine is None or machine.address != service_address(config, "vaultwarden"):
            raise RuntimeError("Vaultwarden sync can only run on the controller or Vaultwarden service host")
        return _sync_catalog_local(root, config, secrets)
    if config.is_multi_node:
        return _sync_catalog_via_tunnel(root, config, secrets)
    return _sync_catalog_local(root, config, secrets)


def _sync_catalog_via_tunnel(root: Path, config: Config, secrets: dict[str, str]) -> list[str]:
    """Sync controller-owned credentials through an ephemeral apps SSH tunnel."""
    from toolkit.core.ansible.ansible_ssh import ssh_local_forward
    from toolkit.core.manifest.placement import service_address, service_route_port

    apps_ip = service_address(config, "vaultwarden")
    remote_port = service_route_port("vaultwarden", published=True)
    with ssh_local_forward(config, root, apps_ip, remote_port, remote_host=apps_ip) as local_port:
        return _sync_catalog_local(
            root,
            config,
            secrets,
            base_url=f"http://127.0.0.1:{local_port}",
        )


def _sync_catalog_local(
    root: Path,
    config: Config,
    secrets: dict[str, str],
    *,
    base_url: str | None = None,
) -> list[str]:
    """Sync credential catalog into the owner's personal vault with client-side encryption."""
    logs: list[str] = []
    master = secrets.get("VAULTWARDEN_MASTER_PASSWORD", "")
    if not master:
        logs.append("Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip sync")
        return logs

    logs.extend(ensure_vaultwarden_account(config, secrets, root, base_url=base_url))

    email = (config.email or f"admin@{config.domain}").strip().lower()
    base = (base_url or vaultwarden_url(config)).rstrip("/")
    kdf = vaultwarden_fetch_kdf(base, email)
    token = vaultwarden_login_access_token(base, email, master, kdf=kdf)
    if not token:
        logs.append("Vaultwarden: cannot login for sync")
        raise RuntimeError("Vaultwarden sync failed: cannot login")

    headers = {
        "Authorization": f"Bearer {token}",
        "Bitwarden-Client-Version": BITWARDEN_CLIENT_VERSION,
        "Content-Type": "application/json",
    }

    try:
        sync_resp = httpx.get(f"{base}/api/sync?excludeDomains=true", headers=headers, timeout=30)
        sync_resp.raise_for_status()
        sync_data = sync_resp.json()
    except httpx.HTTPError as exc:
        logs.append(f"Vaultwarden: sync fetch failed ({exc})")
        raise RuntimeError("Vaultwarden sync failed: cannot fetch vault state") from exc

    profile_key = (sync_data.get("profile") or {}).get("key", "")
    if not profile_key:
        logs.append("Vaultwarden: profile missing protected symmetric key")
        raise RuntimeError("Vaultwarden sync failed: missing profile key")

    try:
        enc_key, mac_key = unlock_account_keys(master, email, profile_key, kdf)
        _persist_bitwarden_keys(root, enc_key, mac_key)
    except (ValueError, subprocess.CalledProcessError) as exc:
        logs.append(f"Vaultwarden: cannot unlock account keys ({exc})")
        raise RuntimeError("Vaultwarden sync failed: cannot unlock account keys") from exc

    existing_by_name = _index_personal_ciphers_by_name(sync_data.get("ciphers") or [], enc_key, mac_key)

    expected = _expected_catalog_count(config, secrets, root)
    synced = 0

    for entry in credential_entries(config):
        value = _resolve_credential_value(entry, secrets, root, config)
        if not value:
            continue
        username = resolve_username(entry, secrets, config)
        cipher = _build_encrypted_cipher(entry, value, username, enc_key, mac_key)

        cipher_id = existing_by_name.get(entry.name)
        try:
            if cipher_id:
                cipher["id"] = cipher_id
                resp = httpx.put(f"{base}/api/ciphers/{cipher_id}", headers=headers, json=cipher, timeout=15)
            else:
                resp = httpx.post(f"{base}/api/ciphers", headers=headers, json=cipher, timeout=15)
            if resp.status_code in (200, 201):
                synced += 1
            else:
                logs.append(f"Vaultwarden: cipher {entry.name!r} returned {resp.status_code}")
        except httpx.HTTPError as exc:
            logs.append(f"Vaultwarden: cipher {entry.name!r} failed ({exc})")

    logs.append(f"Vaultwarden: synced {synced}/{expected} ciphers (encrypted, personal)")
    if synced < expected:
        logs.append(f"Vaultwarden: ERROR sync incomplete ({synced}/{expected})")
        raise RuntimeError(f"Vaultwarden sync incomplete: {synced}/{expected} ciphers")
    import_path = root / "generated" / "vaultwarden-import.json"
    if import_path.is_file():
        import_path.unlink()
        logs.append("Vaultwarden: removed vaultwarden-import.json after import")
    return logs
