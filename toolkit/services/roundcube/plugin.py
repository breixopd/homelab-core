"""roundcube service plugin — webmail client for Docker-Mailserver."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class RoundcubePlugin(ServicePlugin):
    service = "roundcube"
    category = "email"

    def verify(self, cfg: Config, secrets: dict, vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Login page, installer disabled, IMAP reachability, and auth route contract."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            ssh_on_vm,
        )

        if not cfg.category_enabled("email"):
            return [VerifyCheck("roundcube", "login_page", True, "email not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("roundcube", "login_page", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "roundcube", root):
            return [VerifyCheck("roundcube", "container", False, "enabled Roundcube container is missing")]

        checks: list[VerifyCheck] = []
        checks.append(self._check_login_page(cfg, vm_ip, root, docker_curl, ssh_on_vm))
        checks.append(self._check_installer_disabled(cfg, vm_ip, root, docker_curl))
        checks.append(self._check_imap_backend(cfg, vm_ip, root, docker_exec_on_vm, secrets))
        checks.append(self._check_forward_auth_contract(cfg))
        return checks

    def _check_forward_auth_contract(self, cfg: Config) -> VerifyCheck:
        """Ensure the enabled service manifest declares the auth boundary we deploy.

        Roundcube does not implement an OIDC client.  Its public route is
        protected by Caddy's Authelia forward-auth handler, so probing for
        application-level ``oauth_*`` settings incorrectly reported a skipped
        check.  The framework separately probes the live redirect; this
        service-owned check validates that the compiled route still carries
        the declared contract before that probe runs.
        """
        from toolkit.core.manifest.routes import compile_routes
        from toolkit.services.sdk import VerifyCheck

        routes = [route for route in compile_routes(cfg) if route.service == self.service and route.match is None]
        if not routes:
            return VerifyCheck("roundcube", "forward_auth_contract", False, "manifest has no default mail route")
        if len(routes) != 1:
            return VerifyCheck(
                "roundcube",
                "forward_auth_contract",
                False,
                f"manifest has {len(routes)} default mail routes; expected exactly one",
            )
        route = routes[0]
        if route.auth.mode != "forward_auth":
            return VerifyCheck(
                "roundcube",
                "forward_auth_contract",
                False,
                f"mail.{cfg.domain} declares auth mode {route.auth.mode!r}, expected 'forward_auth'",
            )
        if not route.upstream:
            return VerifyCheck(
                "roundcube",
                "forward_auth_contract",
                False,
                f"mail.{cfg.domain} has no application upstream",
            )
        return VerifyCheck(
            "roundcube",
            "forward_auth_contract",
            True,
            f"mail.{cfg.domain} -> {route.upstream} protected by forward_auth",
        )

    def _check_login_page(self, cfg, vm_ip, root, docker_curl, ssh_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        mail_host = f"mail.{cfg.domain}"
        rc, body = docker_curl(cfg, vm_ip, "roundcube", "http://localhost/", root=root, timeout=12)
        if rc == 0 and body:
            ok = any(marker in body.lower() for marker in ("roundcube", "login", "username", "password"))
            return VerifyCheck(
                "roundcube",
                "login_page",
                ok,
                "login page reachable" if ok else "unexpected page content",
            )
        shell = (
            f"curl -skI --max-time 12 --resolve {mail_host}:443:127.0.0.1 "
            f"-H 'X-Forwarded-Proto: https' https://{mail_host}/ 2>&1 | head -10"
        )
        rc2, out, _ = ssh_on_vm(cfg, vm_ip, shell, root=root, timeout=15)
        ok = rc2 == 0 and "200" in (out or "")
        return VerifyCheck(
            "roundcube",
            "login_page",
            ok,
            "login page via caddy" if ok else (out or "unreachable")[:120],
        )

    def _check_installer_disabled(self, cfg, vm_ip, root, docker_curl) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, body = docker_curl(cfg, vm_ip, "roundcube", "http://localhost/installer/", root=root, timeout=10)
        if rc != 0:
            return VerifyCheck("roundcube", "installer_disabled", True, "installer not reachable")
        blocked = "installer" not in (body or "").lower() or "disabled" in (body or "").lower()
        return VerifyCheck(
            "roundcube",
            "installer_disabled",
            blocked,
            "installer disabled" if blocked else "installer still exposed",
        )

    def _check_imap_backend(self, cfg, vm_ip, root, docker_exec_on_vm, secrets) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, out = docker_exec_on_vm(
            cfg,
            "roundcube",
            [
                "sh",
                "-c",
                "timeout 5 bash -c 'echo > /dev/tcp/mailserver/143' 2>/dev/null && echo IMAP_OK",
            ],
            vm_ip,
            root,
            timeout=15,
        )
        if rc == 0 and "IMAP_OK" in (out or ""):
            return VerifyCheck("roundcube", "imap_backend", True, "mailserver:143 reachable from roundcube")
        return VerifyCheck(
            "roundcube",
            "imap_backend",
            False,
            (out or "mailserver:143 not reachable from roundcube")[:120],
        )
