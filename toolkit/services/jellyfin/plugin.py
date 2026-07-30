"""jellyfin service plugin.

Owns its verify() (PLC libraries, LDAP-Auth config, expected plugins) on top
of the base ServicePlugin defaults read from its manifest and Compose application.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class JellyfinPlugin(ServicePlugin):
    service = "jellyfin"
    category = "media"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Run the Jellyfin startup wizard + create libraries + reconcile the API key.

        Raises if the bootstrap reports an authentication failure or restart
        timeout — a stale config/admin password that the wizard reset can't
        recover from without operator intervention.
        """
        import importlib

        from toolkit.services.sdk import resolve_bootstrap_password

        bootstrap = importlib.import_module("toolkit.services.jellyfin.bootstrap")
        logs = bootstrap.bootstrap_jellyfin(cfg, secrets, root=root)
        admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
        if admin_pass:
            for line in logs:
                lower = line.lower()
                if "authentication failed" in lower or "restart timed out" in lower:
                    raise RuntimeError(f"Jellyfin bootstrap failed: {line}")
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, resolve_bootstrap_password

        if service_setting_str(cfg, "media-library", "server") not in ("jellyfin", "both"):
            return [VerifyCheck("jellyfin", "server", True, "not applicable (plex only)")]
        checks = [
            self._check_health(cfg, vm_ip, root),
            self._check_libraries(cfg, secrets, vm_ip, root, resolve_bootstrap_password),
            self._check_ldap(cfg, vm_ip, root),
            self._check_plugins(cfg, secrets, vm_ip, root, resolve_bootstrap_password),
            self._check_ldap_plugin_active(cfg, secrets, vm_ip, root, resolve_bootstrap_password),
            self._check_directory_login(cfg, secrets, vm_ip, root),
            self._check_hw_transcode(cfg, vm_ip, root),
        ]
        return checks

    def _check_health(self, cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """Unauthenticated ``GET /health`` — catches DB failures libraries check might miss."""
        import httpx
        from toolkit.services.sdk import VerifyCheck, docker_curl

        if cfg.is_multi_node:
            rc, body = docker_curl(cfg, vm_ip, "jellyfin", "http://localhost:8096/health", root=root)
            ok = rc == 0 and bool((body or "").strip())
            return VerifyCheck("jellyfin", "health", ok, "healthy" if ok else "unreachable")
        try:
            resp = httpx.get("http://localhost:8096/health", timeout=10)
            return VerifyCheck("jellyfin", "health", resp.status_code == 200, f"HTTP {resp.status_code}")
        except httpx.HTTPError:
            return VerifyCheck("jellyfin", "health", False, "unreachable")

    def _check_hw_transcode(self, cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """When HW transcode is configured, encoding.xml must enable hardware acceleration."""
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, docker_exec_on_vm

        hw = service_setting_str(cfg, self.service, "hardware-transcode")
        if hw in ("none", "auto"):
            return VerifyCheck("jellyfin", "hw_transcode", True, f"skipped ({hw})")
        rc, out = docker_exec_on_vm(cfg, "jellyfin", ["cat", "/config/config/encoding.xml"], vm_ip, root)
        if rc != 0:
            return VerifyCheck("jellyfin", "hw_transcode", False, "encoding.xml missing")
        body = (out or "").lower()
        if hw == "vaapi":
            ok = "vaapi" in body and "enablehardwareencoding>true" in body.replace(" ", "")
        elif hw == "nvidia":
            ok = ("nvenc" in body or "nvidia" in body) and "enablehardwareencoding>true" in body.replace(" ", "")
        else:
            ok = "enablehardwareencoding>true" in body.replace(" ", "")
        return VerifyCheck(
            "jellyfin",
            "hw_transcode",
            ok,
            f"{hw} acceleration enabled" if ok else f"{hw} configured but encoding.xml not set",
        )

    def _check_ldap_plugin_active(self, cfg, secrets, vm_ip, root, resolve_bootstrap_password) -> VerifyCheck:
        """LDAP Authentication plugin installed and active (not just configured on disk)."""
        from toolkit.services.sdk import VerifyCheck

        admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
        api_key = secrets.get("JELLYFIN_API_KEY", "")
        if not admin_pass and not api_key:
            return VerifyCheck("jellyfin", "ldap_active", True, "skipped (no credentials)")
        plugins = self._fetch_plugins(cfg, secrets, vm_ip, root, resolve_bootstrap_password)
        if plugins is None:
            return VerifyCheck("jellyfin", "ldap_active", False, "plugins API unreachable")
        ldap_plugins = [p for p in plugins if isinstance(p, dict) and "ldap" in str(p.get("Name", "")).lower()]
        if not ldap_plugins:
            return VerifyCheck("jellyfin", "ldap_active", False, "LDAP plugin not installed")
        active = any(str(p.get("Status", "")).lower() in ("active", "enabled", "running") for p in ldap_plugins)
        return VerifyCheck(
            "jellyfin",
            "ldap_active",
            active,
            "LDAP plugin active" if active else "LDAP plugin installed but not active",
        )

    def _fetch_plugins(self, cfg, secrets, vm_ip, root, resolve_bootstrap_password) -> list | None:
        """Return Jellyfin plugin list or None when unreachable."""
        import time

        from toolkit.services.sdk import docker_curl

        admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
        api_key = secrets.get("JELLYFIN_API_KEY", "")
        auth_header = 'MediaBrowser Client="verify", Device="cli", DeviceId="hook-verify", Version="1.0"'

        def _login_token() -> str:
            if not admin_pass:
                return ""
            rc_t, out_t = docker_curl(
                cfg,
                vm_ip,
                "jellyfin",
                "http://127.0.0.1:8096/Users/AuthenticateByName",
                root=root,
                method="POST",
                headers={"Content-Type": "application/json", "X-Emby-Authorization": auth_header},
                body=json.dumps({"Username": "admin", "Pw": admin_pass}),
                timeout=30,
            )
            if rc_t == 0 and (out_t or "").strip():
                try:
                    return json.loads(out_t).get("AccessToken", "")
                except json.JSONDecodeError:
                    return ""
            return ""

        auth_token = api_key or _login_token()
        for attempt in range(4):
            if not auth_token:
                return None
            rc, body = docker_curl(
                cfg,
                vm_ip,
                "jellyfin",
                "http://127.0.0.1:8096/Plugins",
                root=root,
                headers={"X-Emby-Token": auth_token},
                timeout=30,
            )
            if rc == 0 and (body or "").strip():
                try:
                    plugins = json.loads(body)
                except json.JSONDecodeError:
                    return None
                if isinstance(plugins, list):
                    return plugins
                return None
            if api_key and admin_pass and attempt == 0:
                auth_token = _login_token()
                continue
            if attempt < 3:
                time.sleep(3)
        return None

    def _check_ldap(self, cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """Jellyfin LDAP Authentication plugin enabled and pointed at LLDAP."""
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, docker_exec_on_vm

        if service_setting_str(cfg, "media-library", "server") not in ("jellyfin", "both"):
            return VerifyCheck("jellyfin", "ldap", True, "not applicable (plex only)")
        from toolkit.services.jellyfin.extras import LDAP_CONFIG_PATH

        rc, out = docker_exec_on_vm(cfg, "jellyfin", ["cat", LDAP_CONFIG_PATH], vm_ip, root)
        if rc != 0:
            return VerifyCheck("jellyfin", "ldap", False, "LDAP-Auth.xml missing — re-run media hooks")
        body = out or ""
        compact = body.replace(" ", "")
        required = (
            "<LdapServer>",
            "<LdapBindUser>",
            "<CreateUsersFromLdap>true</CreateUsersFromLdap>",
            "<LdapUidAttribute>uid</LdapUidAttribute>",
        )
        obsolete = ("<LdapBindDn>", "<EnableLdapAuthentication>", "<CreateUserOnAuthentication>")
        ok = all(value in compact for value in required) and not any(value in compact for value in obsolete)
        return VerifyCheck(
            "jellyfin",
            "ldap",
            ok,
            "LDAP auth uses the active plugin schema" if ok else "LDAP plugin configuration schema is stale",
        )

    def _check_directory_login(
        self,
        cfg: Config,
        secrets: dict[str, str],
        vm_ip: str,
        root: Path,
    ) -> VerifyCheck:
        """Prove that a real directory user can complete Jellyfin login."""
        from toolkit.services.sdk import VerifyCheck, docker_curl

        password = (secrets.get("SSO_USER_PASSWORD") or "").strip()
        email = (cfg.email or "").strip().lower()
        username = email.split("@", 1)[0] if "@" in email else email
        if not password or not username:
            return VerifyCheck(
                "jellyfin",
                "directory_login",
                False,
                "owner directory credentials are unavailable for the user-journey check",
            )

        auth_header = 'MediaBrowser Client="verify", Device="cli", DeviceId="directory-login", Version="1.0"'
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "jellyfin",
            "http://127.0.0.1:8096/Users/AuthenticateByName",
            root=root,
            method="POST",
            headers={"Content-Type": "application/json", "X-Emby-Authorization": auth_header},
            body=json.dumps({"Username": username, "Pw": password}),
            timeout=30,
        )
        ok = rc == 0 and bool((body or "").strip())
        if ok:
            try:
                ok = bool(json.loads(body).get("AccessToken"))
            except (json.JSONDecodeError, AttributeError):
                ok = False
        return VerifyCheck(
            "jellyfin",
            "directory_login",
            ok,
            f"directory user {username} authenticated" if ok else f"directory user {username} login failed",
        )

    def _check_libraries(self, cfg, secrets, vm_ip, root, resolve_bootstrap_password) -> VerifyCheck:
        """Check Jellyfin has library endpoints configured.

        /Library/VirtualFolders requires admin auth, so authenticate with the
        admin credentials instead of trusting a possibly-stale stored API key.
        """
        import httpx
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, ssh_on_vm

        if service_setting_str(cfg, "media-library", "server") not in ("jellyfin", "both"):
            return VerifyCheck("jellyfin", "libraries", True, "not applicable (plex only)")
        admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
        api_key = secrets.get("JELLYFIN_API_KEY", "")
        if not admin_pass and not api_key:
            return VerifyCheck("jellyfin", "libraries", False, "JELLYFIN_ADMIN_PASSWORD or JELLYFIN_API_KEY not set")
        auth_header = 'MediaBrowser Client="verify", Device="cli", DeviceId="hook-verify", Version="1.0"'

        if cfg.is_multi_node:
            from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
            from toolkit.core.manifest.placement import service_node, service_route_port

            jellyfin_node = service_node(cfg, "jellyfin")
            jellyfin_port = service_route_port("jellyfin")

            remote = (
                f"export HOMELAB_NODE={jellyfin_node} && "
                f"cd {DEFAULT_HOMELAB_ROOT} && .venv/bin/python3 -c "
                + shlex.quote(
                    "import json; "
                    "from pathlib import Path; "
                    "from toolkit.core.config.config import load_config, config_path; "
                    "from toolkit.core.config.storage import secrets_path; "
                    "from toolkit.core.secrets.secrets import load_runtime_secrets; "
                    "from toolkit.core.ops.automation import resolve_docker_service_url; "
                    "from toolkit.services.jellyfin.bootstrap import jellyfin_auth_token; "
                    "import httpx; "
                    f"root = Path({DEFAULT_HOMELAB_ROOT!r}); "
                    "cfg = load_config(config_path(root)); "
                    f"secrets = load_runtime_secrets(root, role={jellyfin_node!r}); "
                    f"base = resolve_docker_service_url('jellyfin', {jellyfin_port}); "
                    "bp = __import__('toolkit.core.secrets.bootstrap_passwords', "
                    "fromlist=['resolve_bootstrap_password']); "
                    "pw = bp.resolve_bootstrap_password(secrets, 'JELLYFIN_ADMIN_PASSWORD'); "
                    "token = jellyfin_auth_token(base, 'admin', pw); "
                    "headers = {'X-Emby-Authorization': f'MediaBrowser Token=\"{token}\"'} if token else {}; "
                    "resp = httpx.get(f'{base}/Library/VirtualFolders', headers=headers, timeout=15); "
                    "print(json.dumps({'status': resp.status_code, 'body': resp.text}))"
                )
            )
            rc, body, _ = ssh_on_vm(cfg, vm_ip, remote, root=root, timeout=60)
            if rc != 0 or not (body or "").strip():
                return VerifyCheck("jellyfin", "libraries", False, "library API unreachable")
            try:
                payload = json.loads(body.strip().splitlines()[-1])
                if payload.get("status") != 200:
                    return VerifyCheck(
                        "jellyfin", "libraries", False, f"HTTP {payload.get('status')} (re-run deploy hooks on media)"
                    )
                folders = json.loads(payload.get("body") or "[]")
            except (json.JSONDecodeError, TypeError):
                return VerifyCheck("jellyfin", "libraries", False, "API returned invalid JSON (auth failed?)")
        else:
            try:
                login = httpx.post(
                    "http://localhost:8096/Users/AuthenticateByName",
                    json={"Username": "admin", "Pw": admin_pass},
                    headers={"X-Emby-Authorization": auth_header},
                    timeout=10,
                )
                if login.status_code != 200:
                    return VerifyCheck("jellyfin", "libraries", False, f"admin auth failed (HTTP {login.status_code})")
                token = login.json().get("AccessToken", "")
                resp = httpx.get(
                    "http://localhost:8096/Library/VirtualFolders", headers={"X-Emby-Token": token}, timeout=10
                )
                if resp.status_code != 200:
                    return VerifyCheck("jellyfin", "libraries", False, f"HTTP {resp.status_code}")
                folders = resp.json()
            except httpx.HTTPError:
                return VerifyCheck("jellyfin", "libraries", False, "API unreachable")

        count = len(folders) if isinstance(folders, list) else 0
        names = [f.get("Name", "") for f in (folders or []) if isinstance(f, dict)]
        libs_detail = ", ".join(names) if names else "none"
        return VerifyCheck("jellyfin", "libraries", count > 0, f"{count} library(ies): {libs_detail}")

    def _check_plugins(self, cfg, secrets, vm_ip, root, resolve_bootstrap_password) -> VerifyCheck:
        """Verify the expected Jellyfin plugins, including the optional cache webhook."""
        from toolkit.core.manifest.settings import service_enabled, service_setting_str
        from toolkit.services.sdk import VerifyCheck

        if service_setting_str(cfg, "media-library", "server") not in ("jellyfin", "both"):
            return VerifyCheck("jellyfin", "plugins", True, "not applicable (plex only)")
        admin_pass = resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD")
        api_key = secrets.get("JELLYFIN_API_KEY", "")
        if not admin_pass and not api_key:
            return VerifyCheck("jellyfin", "plugins", False, "JELLYFIN_ADMIN_PASSWORD or JELLYFIN_API_KEY not set")
        expected: list[str] = ["Intro Skipper", "LDAP-Auth"]
        if service_enabled(cfg, "media-cache"):
            expected.append("Webhook")

        plugins = self._fetch_plugins(cfg, secrets, vm_ip, root, resolve_bootstrap_password)
        if plugins is None:
            return VerifyCheck("jellyfin", "plugins", False, "plugins API unreachable")

        installed = {str(p.get("Name", "")).strip() for p in (plugins or []) if isinstance(p, dict) and p.get("Name")}
        missing = [name for name in expected if name not in installed]
        if missing:
            detail = f"missing: {', '.join(missing)} (installed: {', '.join(sorted(installed)) or 'none'})"
            return VerifyCheck("jellyfin", "plugins", False, detail)
        detail = f"{len(expected)} expected plugin(s) present: {', '.join(expected)}"
        return VerifyCheck("jellyfin", "plugins", True, detail)
