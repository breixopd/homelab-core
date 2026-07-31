"""vaultwarden service plugin.

ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml; this file overrides the hooks that
need custom Python logic (verify, post_start, oidc_client).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
from toolkit.services import IdentityProvisionResult, OIDCClient, RuntimeEnvironmentContext, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class VaultwardenPlugin(ServicePlugin):
    service = "vaultwarden"
    category = "cloud"

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        """Expose the bounded, read-only Vaultwarden/Postgres readiness probe.

        ``/alive`` does not require credentials and is the same endpoint used by
        the service health contract.  Keep the result numeric because management
        status metrics are intentionally scalar and allow-listed by the manifest.
        """
        from toolkit.services.sdk import docker_curl

        rc, body = docker_curl(
            cfg,
            self.runtime_address(cfg),
            self.service,
            "http://localhost/alive",
            root=root,
            timeout=10,
        )
        return {"readiness": 1} if rc == 0 and bool((body or "").strip()) else {}

    def runtime_environment(self, context: RuntimeEnvironmentContext) -> dict[str, str]:
        """Hash the stored administration token for Vaultwarden's runtime."""
        from toolkit.core.secrets.bitwarden_crypto import stable_vaultwarden_admin_hash

        token = context.secrets.get("VAULTWARDEN_ADMIN_TOKEN", "")
        if not token or token.startswith("$argon2"):
            return {"VAULTWARDEN_ADMIN_TOKEN": token}
        previous = context.previous.get("VAULTWARDEN_ADMIN_TOKEN", "").replace("$$", "$")
        return {"VAULTWARDEN_ADMIN_TOKEN": stable_vaultwarden_admin_hash(token, previous)}

    @property
    def oidc_client(self) -> OIDCClient:
        return OIDCClient(
            client_id="vaultwarden",
            secret_env_var="SSO_CLIENT_SECRET",
            native=True,  # OIDC-native (no forward-auth)
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Sync credentials into Vaultwarden (idempotent)."""
        if root is None:
            raise RuntimeError("Vaultwarden sync requires the deployment root")
        from toolkit.services.vaultwarden.bootstrap import sync_catalog_to_vaultwarden

        return sync_catalog_to_vaultwarden(root, cfg, secrets)

    def provision_identity(
        self,
        cfg: Config,
        secrets: dict[str, str],
        email: str,
        *,
        root: Path | None = None,
    ) -> tuple[IdentityProvisionResult, ...]:
        """Create the Vaultwarden invitation required before first signup."""
        from toolkit.services.sdk import vaultwarden_admin_session, vaultwarden_url

        key = "vaultwarden_invite"
        admin_token = secrets.get("VAULTWARDEN_ADMIN_TOKEN", "")
        if not admin_token:
            return (
                IdentityProvisionResult(
                    key,
                    "failed",
                    "Vaultwarden: VAULTWARDEN_ADMIN_TOKEN missing; invite not created",
                ),
            )
        base = vaultwarden_url(cfg)
        cookies = vaultwarden_admin_session(base, admin_token)
        if cookies is None:
            return (IdentityProvisionResult(key, "failed", "Vaultwarden: admin login failed"),)
        normalized_email = email.strip().lower()
        try:
            response = httpx.get(f"{base}/admin/users", cookies=cookies, timeout=15)
            users = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            users = None
        if not isinstance(users, list):
            return (IdentityProvisionResult(key, "failed", "Vaultwarden: existing-user lookup unavailable"),)
        if any((user.get("email") or "").strip().lower() == normalized_email for user in users):
            return (
                IdentityProvisionResult(
                    key,
                    "completed",
                    f"Vaultwarden: {normalized_email} already has an account",
                ),
            )
        try:
            response = httpx.post(
                f"{base}/admin/invite",
                cookies=cookies,
                json={"email": normalized_email},
                timeout=15,
            )
        except httpx.HTTPError:
            return (IdentityProvisionResult(key, "failed", "Vaultwarden: invite request failed"),)
        if response.status_code in (200, 201, 204):
            return (
                IdentityProvisionResult(
                    key,
                    "completed",
                    f"Vaultwarden: invitation created for {normalized_email}",
                ),
            )
        return (
            IdentityProvisionResult(
                key,
                "failed",
                f"Vaultwarden: invite HTTP {response.status_code} for {normalized_email}",
            ),
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """OIDC issuer parity, DB /alive probe, and admin session."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            oidc_check_env_issuer,
            vaultwarden_admin_session,
            vaultwarden_fetch_kdf,
            vaultwarden_login_access_token,
            vaultwarden_url,
        )

        checks = oidc_check_env_issuer(cfg, "vaultwarden", "vaultwarden", "SSO_AUTHORITY", vm_ip, root)

        if cfg.domain == "localhost":
            checks.append(VerifyCheck("vaultwarden", "alive", True, "skipped (localhost)"))
            checks.append(VerifyCheck("vaultwarden", "admin_session", True, "skipped (localhost)"))
            checks.append(VerifyCheck("vaultwarden", "owner_login", True, "skipped (localhost)"))
            return checks

        if not container_exists_on_vm(cfg, vm_ip, "vaultwarden", root):
            checks.append(VerifyCheck("vaultwarden", "alive", False, "container missing"))
            checks.append(VerifyCheck("vaultwarden", "admin_session", False, "container missing"))
            return checks

        # ── alive — proves Postgres connectivity, not just process liveness ───
        rc, body = docker_curl(cfg, vm_ip, "vaultwarden", "http://localhost/alive", root=root, timeout=12)
        alive_ok = rc == 0 and bool((body or "").strip())
        checks.append(
            VerifyCheck(
                "vaultwarden",
                "alive",
                alive_ok,
                "DB reachable" if alive_ok else ((body or "unreachable")[:120]),
            )
        )

        # ── admin_session — admin token yields a valid session cookie ─────────
        admin_token = secrets.get("VAULTWARDEN_ADMIN_TOKEN", "")
        if not admin_token:
            checks.append(VerifyCheck("vaultwarden", "admin_session", False, "VAULTWARDEN_ADMIN_TOKEN not set"))
        else:
            if cfg.is_multi_node:
                # Controller-side verification cannot reach guest private
                # ports directly. Probe the published endpoint from inside
                # the service container instead; curl's ``fail`` mode makes
                # an invalid token a non-zero result without exposing it in
                # process arguments or logs.
                rc, _body = docker_curl(
                    cfg,
                    vm_ip,
                    "vaultwarden",
                    "http://localhost/admin",
                    root=root,
                    method="POST",
                    body=urlencode({"token": admin_token}),
                    timeout=15,
                )
                session_ok = rc == 0
            else:
                base = vaultwarden_url(cfg)
                session_ok = vaultwarden_admin_session(base, admin_token) is not None
            checks.append(
                VerifyCheck(
                    "vaultwarden",
                    "admin_session",
                    session_ok,
                    "admin token accepted" if session_ok else "admin login failed",
                )
            )

        master_password = secrets.get("VAULTWARDEN_MASTER_PASSWORD", "")
        owner_email = (cfg.email or f"admin@{cfg.domain}").strip().lower()
        owner_login = False
        if master_password and cfg.is_multi_node:
            from toolkit.core.secrets.bitwarden_crypto import kdf_from_prelogin, make_master_password_hash
            from toolkit.services.sdk import BITWARDEN_CLIENT_VERSION

            rc, prelogin_body = docker_curl(
                cfg,
                vm_ip,
                "vaultwarden",
                "http://localhost/identity/accounts/prelogin",
                root=root,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"email": owner_email}),
                timeout=15,
            )
            if rc == 0 and prelogin_body:
                try:
                    kdf = kdf_from_prelogin(json.loads(prelogin_body))
                    password_hash = make_master_password_hash(master_password, owner_email, kdf)
                    rc, login_body = docker_curl(
                        cfg,
                        vm_ip,
                        "vaultwarden",
                        "http://localhost/identity/connect/token",
                        root=root,
                        method="POST",
                        headers={
                            "Bitwarden-Client-Version": BITWARDEN_CLIENT_VERSION,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        body=urlencode(
                            {
                                "grant_type": "password",
                                "scope": "api offline_access",
                                "client_id": "web",
                                "username": owner_email,
                                "password": password_hash,
                                "deviceType": "14",
                                "deviceName": "homelab-toolkit-verify",
                                "deviceIdentifier": str(
                                    uuid.uuid5(uuid.NAMESPACE_DNS, f"homelab-toolkit:{owner_email}")
                                ),
                            }
                        ),
                        timeout=20,
                    )
                    owner_login = rc == 0 and bool(json.loads(login_body or "{}").get("access_token"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    owner_login = False
        elif master_password:
            kdf = vaultwarden_fetch_kdf(vaultwarden_url(cfg), owner_email)
            owner_login = bool(
                vaultwarden_login_access_token(
                    vaultwarden_url(cfg),
                    owner_email,
                    master_password,
                    kdf=kdf,
                )
            )
        checks.append(
            VerifyCheck(
                "vaultwarden",
                "owner_login",
                owner_login,
                "owner password login accepted" if owner_login else "owner password login failed",
            )
        )
        return checks
