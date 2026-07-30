"""Jellyfin post-deploy bootstrap: startup wizard, libraries, and API key reconcile."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from defusedxml import ElementTree
from toolkit.core.ops.automation import docker_exec, resolve_docker_service_url
from toolkit.services.sdk import resolve_bootstrap_password

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
logger = logging.getLogger(__name__)


def jellyfin_auth_token(
    base_url: str,
    username: str,
    password: str,
    *,
    client: str = "homelab-toolkit",
) -> str | None:
    """Return a Jellyfin access token, or None when authentication fails."""
    auth_header = f'MediaBrowser Client="{client}", Device="CLI", DeviceId="auto", Version="1.0"'
    try:
        response = httpx.post(
            f"{base_url}/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            headers={"X-Emby-Authorization": auth_header},
            timeout=15,
        )
        if response.status_code != 200:
            return None
        return str(response.json().get("AccessToken") or "") or None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def setup_jellyfin_api_key(
    base_url: str = "http://jellyfin:8096",
    admin_user: str = "admin",
    admin_pass: str = "",
    key_name: str = "homelab-toolkit",
) -> str | None:
    """Authenticate to Jellyfin and reconcile the named API key."""
    auth_header = 'MediaBrowser Client="Script", Device="CLI", DeviceId="auto", Version="1.0"'
    try:
        response = httpx.post(
            f"{base_url}/Users/AuthenticateByName",
            json={"Username": admin_user, "Pw": admin_pass},
            headers={"X-Emby-Authorization": auth_header},
            timeout=15,
        )
        response.raise_for_status()
        token = response.json().get("AccessToken", "")
        if not token:
            return None

        authz = f'MediaBrowser Token="{token}"'
        keys_response = httpx.get(
            f"{base_url}/Auth/Keys",
            headers={"X-Emby-Authorization": authz},
            timeout=15,
        )
        keys_response.raise_for_status()
        items = keys_response.json().get("Items", [])
        if not isinstance(items, list):
            return None
        # Reuse the named key whenever possible.  Creating a new key on every
        # hook run leaks credentials into Jellyfin's database and invalidates
        # the controller's stored value after a restart.
        for item in reversed(items):
            if isinstance(item, dict) and item.get("AppName") == key_name:
                api_key = item.get("AccessToken") or item.get("Key")
                if api_key:
                    return str(api_key)

        key_response = httpx.post(
            f"{base_url}/Auth/Keys",
            params={"app": key_name},
            headers={"X-Emby-Authorization": authz},
            timeout=15,
        )
        if key_response.status_code not in (200, 201, 204):
            key_response.raise_for_status()
        keys_response = httpx.get(
            f"{base_url}/Auth/Keys",
            headers={"X-Emby-Authorization": authz},
            timeout=15,
        )
        keys_response.raise_for_status()
        items = keys_response.json().get("Items", [])
        if not isinstance(items, list):
            return None
        for item in reversed(items):
            if isinstance(item, dict) and item.get("AppName") == key_name:
                api_key = item.get("AccessToken") or item.get("Key")
                if api_key:
                    return str(api_key)
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return None


def _jellyfin_config_dir(root: Path | None) -> Path | None:
    if root is None:
        return None
    return root / "config" / "jellyfin"


def _reset_jellyfin_startup_wizard(config_dir: Path) -> bool:
    """Allow Startup API to run again when admin password drifted from secrets."""
    system_xml = config_dir / "system.xml"
    if not system_xml.is_file():
        return False
    tree = ElementTree.parse(system_xml)
    elem = tree.find("IsStartupWizardCompleted")
    if elem is None or (elem.text or "").lower() != "true":
        return False
    elem.text = "false"
    tree.write(system_xml, encoding="utf-8", xml_declaration=True)
    return True


def _wait_jellyfin_healthy(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, _ = docker_exec("jellyfin", ["curl", "-sf", "http://127.0.0.1:8096/health"])
        if rc == 0:
            return True
        time.sleep(5)
    return False


def _jellyfin_run_startup(base: str, admin_pass: str, admin_user: str = "admin") -> list[str]:
    logs: list[str] = []
    httpx.post(
        f"{base}/Startup/Configuration",
        json={"UICulture": "en-US", "MetadataCountryCode": "US"},
        timeout=15,
    )
    placeholder = admin_user
    try:
        first_user = httpx.get(f"{base}/Startup/User", timeout=15)
        if first_user.status_code == 200:
            placeholder = str(first_user.json().get("Name") or admin_user)
    except httpx.HTTPError:
        pass
    user_resp = httpx.post(
        f"{base}/Startup/User",
        json={"Name": admin_user, "Password": admin_pass},
        timeout=15,
    )
    if user_resp.status_code not in (200, 204):
        # Jellyfin 10.11 may only accept updating the placeholder user name.
        user_resp = httpx.post(
            f"{base}/Startup/User",
            json={"Name": placeholder, "Password": admin_pass},
            timeout=15,
        )
    if user_resp.status_code not in (200, 204):
        logs.append(f"Jellyfin: startup user HTTP {user_resp.status_code}")
        return logs
    httpx.post(f"{base}/Startup/Complete", timeout=15)
    logs.append(f"Jellyfin: startup wizard completed via API (user {admin_user})")
    return logs


def bootstrap_jellyfin(
    config: Config,
    secrets: dict[str, str],
    *,
    root: Path | None = None,
) -> list[str]:
    logs: list[str] = []
    from toolkit.core.manifest.settings import service_setting_str

    if service_setting_str(config, "media-library", "server") not in ("jellyfin", "both"):
        return logs
    base = resolve_docker_service_url("jellyfin", 8096)
    admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
    if not admin_pass:
        logs.append("Jellyfin: SSO_USER_PASSWORD not set — skip wizard")
        return logs

    try:
        sys_info = httpx.get(f"{base}/System/Info/Public", timeout=10)
        if sys_info.status_code != 200:
            logs.append("Jellyfin: not ready")
            return logs
        started = sys_info.json().get("StartupWizardCompleted", False)
        token = jellyfin_auth_token(base, "admin", admin_pass)
        if not token and started:
            config_dir = _jellyfin_config_dir(root)
            if config_dir and _reset_jellyfin_startup_wizard(config_dir):
                logs.append("Jellyfin: admin login failed — resetting startup wizard")
                import subprocess

                subprocess.run(["docker", "restart", "jellyfin"], check=False, timeout=60)
                if not _wait_jellyfin_healthy():
                    logs.append("Jellyfin: restart timed out waiting for health")
                    return logs
                started = False
        if not started:
            logs.extend(_jellyfin_run_startup(base, admin_pass))
            token = jellyfin_auth_token(base, "admin", admin_pass)
        if not token:
            logs.append("Jellyfin: admin authentication failed — libraries not configured")
            return logs

        authz = f'MediaBrowser Token="{token}"'
        headers = {"X-Emby-Authorization": authz}
        lib_resp = httpx.get(f"{base}/Library/VirtualFolders", headers=headers, timeout=10)
        existing: set[str] = set()
        if lib_resp.status_code == 200:
            existing = {name for v in lib_resp.json() if isinstance(v, dict) for name in [v.get("Name")] if name}

        for name, path, coll in [
            ("Movies", "/data/media/movies", "movies"),
            ("TV", "/data/media/tv", "tvshows"),
        ]:
            if name in existing:
                continue
            create = httpx.post(
                f"{base}/Library/VirtualFolders",
                headers=headers,
                params={
                    "name": name,
                    "collectionType": coll,
                    "paths": path,
                    "refreshLibrary": "false",
                },
                timeout=15,
            )
            if create.status_code in (200, 201, 204):
                logs.append(f"Jellyfin: library {name} → {path}")
            else:
                logs.append(f"Jellyfin: library {name} failed (HTTP {create.status_code})")

        api_key = setup_jellyfin_api_key(base, "admin", admin_pass)
        if api_key:
            if secrets.get("JELLYFIN_API_KEY") != api_key:
                secrets["JELLYFIN_API_KEY"] = api_key
                _persist_secret(root, "JELLYFIN_API_KEY", api_key)
                logs.append("Jellyfin: API key reconciled into secrets")
            else:
                logs.append("Jellyfin: API key verified")
            from toolkit.services.jellyfin.extras import configure_jellyfin_extras

            logs.extend(
                configure_jellyfin_extras(
                    config,
                    api_key,
                    base_url=base,
                    lldap_bind_password=secrets.get("LLDAP_BIND_PASSWORD", ""),
                )
            )
    except httpx.HTTPError as exc:
        logs.append(f"Jellyfin: bootstrap failed ({exc})")
    return logs


def _persist_secret(root: Path | None, key: str, value: str) -> None:
    """Best-effort write-back of a runtime-discovered secret to the local store."""
    try:
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

        sp = secrets_path(root) if root else None
        if sp is None or not sp.exists():
            return
        stored = load_secrets_plaintext(sp)
        if stored.get(key) == value:
            return
        stored[key] = value
        save_secrets_plaintext(stored, sp)
    except Exception:
        logger.warning("Could not persist %s to secrets store", key, exc_info=True)
