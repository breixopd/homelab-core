"""lldap service plugin.

Owns its post_start() (POSIX schema + service-bind + owner user/groups) on top
of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import FleetOnboardingContribution, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services import FleetOnboardingContext, RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


class LldapPlugin(ServicePlugin):
    service = "lldap"
    category = "management"

    def ansible_secret_variables(self, cfg: Config, secrets: dict[str, str]) -> dict[str, str]:
        """Provide the SSSD bind credential without writing it to generated group vars."""
        return {"lldap_bind_password": secrets.get("LLDAP_BIND_PASSWORD", "")}

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        context.run_recovery("sync_ldap_bind_only", "toolkit.services.lldap.bootstrap")

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Ensure LLDAP POSIX schema, service-bind, owner user and groups."""
        import importlib

        bootstrap = importlib.import_module("toolkit.services.lldap.bootstrap")
        return bootstrap.bootstrap_lldap_user(cfg, secrets, root=root)

    def prepare_fleet_onboarding(
        self,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> FleetOnboardingContribution:
        from toolkit.services.lldap.bootstrap import ensure_fleet_user

        return FleetOnboardingContribution(logs=tuple(ensure_fleet_user(cfg, root, host.lldap_email or cfg.email)))

    def after_fleet_onboarding(self, context: FleetOnboardingContext) -> None:
        from toolkit.core.identity.ldap_automation import ensure_directory_and_sssd

        for line in ensure_directory_and_sssd(context.root, limit=context.host.name, repair=True):
            context.log(line)

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "ldap-client":
            raise ValueError(f"unsupported LLDAP host integration: {integration}")
        from toolkit.services.sdk.host_agents import systemd_unit_active

        active = systemd_unit_active(root, host, "sssd")
        if active is True:
            return True, "SSSD active"
        if active is False:
            return False, "SSSD inactive"
        return None, "could not query SSSD"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        import json

        from toolkit.core.identity.lldap_client import POSIX_USERS_GROUP
        from toolkit.services.sdk import (
            VerifyCheck,
            base_dn,
            bind_dn,
            container_exists_on_vm,
            docker_curl,
            ldap_bind_search_on_vm,
            lldap_http_port,
        )

        def _skip(note: str) -> list[VerifyCheck]:
            return [VerifyCheck("lldap", "skipped", True, note)]

        if cfg.domain == "localhost":
            return _skip("skipped (localhost)")
        if not container_exists_on_vm(cfg, vm_ip, "lldap", root):
            return [VerifyCheck("lldap", "health", False, "container missing")]

        checks: list[VerifyCheck] = []

        # ── check_health — HTTP readiness (image has no CLI health subcommand) ─
        http_port = lldap_http_port()
        rc, out = docker_curl(cfg, vm_ip, "lldap", f"http://localhost:{http_port}/health", root=root, timeout=15)
        out_l = (out or "").lower()
        health_ok = (
            rc == 0
            and "oci runtime exec failed" not in out_l
            and (not (out or "").strip() or (out or "").strip().lower() in ("ok", "healthy", "true", "1"))
        )
        checks.append(
            VerifyCheck(
                "lldap",
                "check_health",
                health_ok,
                "ok" if health_ok else (out or f"HTTP {rc}")[:120],
            )
        )

        # ── ldap_bind — service account bind + admin user lookup ──────────────
        bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
        if not bind_password:
            checks.append(VerifyCheck("lldap", "ldap_bind", False, "LLDAP_BIND_PASSWORD not set"))
        else:
            _bind_dn = bind_dn(cfg)
            _base_dn = base_dn(cfg)
            rc, out = ldap_bind_search_on_vm(
                cfg,
                vm_ip,
                root,
                bind_password=bind_password,
                bind_dn_value=_bind_dn,
                base_dn_value=_base_dn,
                search_filter="(uid=admin)",
            )
            lowered = (out or "").lower()
            bind_ok = rc == 0 and ("dn:" in lowered or "cn=admin" in lowered)
            checks.append(
                VerifyCheck(
                    "lldap",
                    "ldap_bind",
                    bind_ok,
                    (
                        "ldap-bind ok; admin entry found"
                        if bind_ok
                        else (out.strip().splitlines()[-1] if out.strip() else f"bind failed (rc={rc})")[:120]
                    ),
                )
            )

        # ── base_dn — expected base DN is searchable ──────────────────────────
        admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
        if not admin_password:
            checks.append(VerifyCheck("lldap", "base_dn", False, "LLDAP_ADMIN_PASSWORD not set"))
        else:
            rc_login, login_out = docker_curl(
                cfg,
                vm_ip,
                "lldap",
                f"http://localhost:{http_port}/auth/simple/login",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"username": "admin", "password": admin_password}),
                root=root,
                timeout=15,
            )
            token = ""
            if rc_login == 0 and login_out:
                try:
                    token = json.loads(login_out).get("token", "")
                except json.JSONDecodeError:
                    pass
            if not token:
                checks.append(VerifyCheck("lldap", "base_dn", False, "admin login failed"))
            else:
                rc_gql, gql_out = docker_curl(
                    cfg,
                    vm_ip,
                    "lldap",
                    f"http://localhost:{http_port}/api/graphql",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    body=json.dumps({"query": "{ groups { displayName } }"}),
                    root=root,
                    timeout=15,
                )
                groups: list[str] = []
                if rc_gql == 0 and gql_out:
                    try:
                        groups = [
                            g.get("displayName", "")
                            for g in (json.loads(gql_out).get("data") or {}).get("groups") or []
                            if isinstance(g, dict)
                        ]
                    except json.JSONDecodeError:
                        pass
                has_users_group = POSIX_USERS_GROUP in groups
                checks.append(
                    VerifyCheck(
                        "lldap",
                        "graphql_groups",
                        has_users_group,
                        f"found {POSIX_USERS_GROUP}" if has_users_group else f"groups={groups!r}",
                    )
                )
                checks.append(
                    VerifyCheck(
                        "lldap",
                        "base_dn",
                        bool(groups),
                        f"base DN {base_dn(cfg)} searchable ({len(groups)} group(s))",
                    )
                )
        return checks
