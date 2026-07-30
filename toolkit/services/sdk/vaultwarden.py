"""Vaultwarden URL and session helpers — cfg-aware."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from toolkit.core.ops.automation import resolve_docker_service_url
from toolkit.core.secrets.bitwarden_crypto import KdfParams, kdf_from_prelogin, make_master_password_hash

BITWARDEN_CLIENT_VERSION = "2026.4.1"

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "vaultwarden_url",
    "vaultwarden_admin_session",
    "vaultwarden_sync_catalog",
    "vaultwarden_fetch_kdf",
    "vaultwarden_login_access_token",
]


def vaultwarden_url(cfg: Config) -> str:
    """Reachable Vaultwarden HTTP base URL."""
    if cfg.is_multi_node:
        from toolkit.core.manifest.placement import service_address, service_route_port

        return f"http://{service_address(cfg, 'vaultwarden')}:{service_route_port('vaultwarden', published=True)}"
    return resolve_docker_service_url("vaultwarden", 80)


def vaultwarden_admin_session(base: str, admin_token: str) -> httpx.Cookies | None:
    """Obtain admin session cookies via ``POST /admin``."""
    if not admin_token:
        return None
    try:
        resp = httpx.post(
            f"{base}/admin",
            data={"token": admin_token},
            follow_redirects=False,
            timeout=15,
        )
        if resp.status_code in (200, 302, 303) and resp.cookies:
            return resp.cookies
    except httpx.HTTPError:
        pass
    return None


def vaultwarden_fetch_kdf(base: str, email: str) -> KdfParams:
    """Fetch KDF params from Vaultwarden prelogin endpoint."""
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/identity/accounts/prelogin",
            json={"email": email.strip().lower()},
            timeout=15,
        )
        if resp.status_code == 200:
            return kdf_from_prelogin(resp.json())
    except httpx.HTTPError:
        pass
    from toolkit.core.secrets.bitwarden_crypto import (
        DEFAULT_KDF,
        DEFAULT_KDF_ITERATIONS,
        DEFAULT_KDF_MEMORY,
        DEFAULT_KDF_PARALLELISM,
    )

    return KdfParams(DEFAULT_KDF, DEFAULT_KDF_ITERATIONS, DEFAULT_KDF_MEMORY, DEFAULT_KDF_PARALLELISM)


def vaultwarden_login_access_token(base: str, email: str, master_password: str, *, kdf: KdfParams) -> str:
    """Password-grant login; returns access token or empty string."""
    password_hash = make_master_password_hash(master_password, email, kdf)
    device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"homelab-toolkit:{email.strip().lower()}"))
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/identity/connect/token",
            headers={"Bitwarden-Client-Version": BITWARDEN_CLIENT_VERSION},
            data={
                "grant_type": "password",
                "scope": "api offline_access",
                "client_id": "web",
                "username": email.strip().lower(),
                "password": password_hash,
                "deviceType": "14",
                "deviceName": "homelab-toolkit",
                "deviceIdentifier": device_id,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token", "")
    except httpx.HTTPError:
        pass
    return ""


def vaultwarden_sync_catalog(root: Path, cfg: Config, secrets: dict[str, str]) -> list[str]:
    """Sync credential catalog into Vaultwarden (delegates to bootstrap module)."""
    from toolkit.services.vaultwarden.bootstrap import sync_catalog_to_vaultwarden

    return sync_catalog_to_vaultwarden(root, cfg, secrets)
