"""gitea service plugin.

Owns its verify() and post_start() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from service.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


def _check_gitea_forward_auth(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Gitea routes behind Authelia forward-auth (30x → auth.<domain>).

    Probes ``git.<domain>`` via caddy with a local resolve, falling back to
    curling from inside the ``caddy`` container; parses the redirect status and
    Location header to confirm Authelia is interposing.
    """
    from toolkit.services.sdk import VerifyCheck, parse_curl_headers, ssh_on_vm

    auth_host = f"auth.{cfg.domain}"
    git_host = f"git.{cfg.domain}"
    shell = (
        f"curl -skI --max-time 10 --resolve {git_host}:443:127.0.0.1 "
        f"-H 'X-Forwarded-Proto: https' https://{git_host}/ 2>&1 || "
        f"docker exec caddy curl -skI --max-time 10 -H 'Host: {git_host}' "
        f"-H 'X-Forwarded-Proto: https' https://127.0.0.1/ 2>&1"
    )
    rc, out, _ = ssh_on_vm(cfg, vm_ip, shell, root=root, timeout=20)
    if rc != 0 and not out:
        return VerifyCheck("gitea", "forward_auth", False, "curl failed")
    status, headers = parse_curl_headers(out)
    location = headers.get("location", "")
    combined = (out or "").lower()
    if status is None and "location:" in combined:
        for line in out.splitlines():
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()
                status = 302
                break
    ok = status in (302, 307, 308) and auth_host in location
    if ok:
        detail = f"HTTP {status} → {location[:80]}"
    elif status is None:
        detail = "could not parse HTTP status"
    else:
        detail = f"HTTP {status}, location={location[:80] or '(missing)'}"
    return VerifyCheck("gitea", "forward_auth", ok, detail)


def _gitea_component_pass(data: dict, component: str) -> bool:
    """Return whether every current Gitea health check for a component passes."""
    checks = data.get("checks") or {}
    prefix = f"{component}:"
    matching = [value for key, value in checks.items() if str(key).startswith(prefix)]
    return bool(matching) and all(
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and item.get("status") == "pass" for item in value)
        for value in matching
    )


class GiteaPlugin(ServicePlugin):
    service = "gitea"
    category = "cloud"

    def reconcile_runtime_credentials(self, cfg: Config, root: Path) -> list[str]:
        import importlib

        bootstrap = importlib.import_module("toolkit.services.gitea.bootstrap")
        return bootstrap.reconcile_gitea_runtime_credentials(cfg, root)

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Gitea admin bootstrap + token generation."""
        from toolkit.core.ops.automation import health_check_logs, resolve_docker_service_url

        logs: list[str] = []
        logs.extend(
            health_check_logs(
                [
                    ("gitea", f"{resolve_docker_service_url('gitea', 3000)}/api/healthz"),
                    ("gitea-registry", f"{resolve_docker_service_url('gitea', 3000)}/v2/"),
                ]
            )
        )
        if root is None:
            root = Path.cwd()
        import importlib

        bootstrap = importlib.import_module("toolkit.services.gitea.bootstrap")
        logs.extend(bootstrap.bootstrap_gitea_admin(cfg, secrets, root=root))
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Healthz, admin API, reverse-proxy auth, SSH clone port, forward-auth."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            ssh_on_vm,
        )

        if not cfg.category_enabled("cloud"):
            return [VerifyCheck("gitea", "healthz", True, "cloud not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("gitea", "healthz", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "gitea", root):
            return [VerifyCheck("gitea", "healthz", False, "container missing")]

        checks: list[VerifyCheck] = []
        checks.append(self._check_healthz(cfg, vm_ip, root, docker_curl))
        checks.append(self._check_admin_api(cfg, vm_ip, root, docker_curl, secrets))
        checks.append(self._check_reverse_proxy_auth(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_ssh_port(cfg, vm_ip, root, docker_exec_on_vm, ssh_on_vm))
        checks.append(_check_gitea_forward_auth(cfg, vm_ip, root))
        return checks

    def _check_healthz(self, cfg, vm_ip, root, docker_curl) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, body = docker_curl(cfg, vm_ip, "gitea", "http://localhost:3000/api/healthz", root=root)
        if rc != 0 or not body:
            return VerifyCheck("gitea", "healthz", False, "healthz unreachable")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return VerifyCheck("gitea", "healthz", False, "invalid healthz JSON")
        status = data.get("status", "")
        db_ok = _gitea_component_pass(data, "database")
        ok = status == "pass" and db_ok
        return VerifyCheck(
            "gitea",
            "healthz",
            ok,
            f"status={status} database={'pass' if db_ok else 'fail'}",
        )

    def _check_admin_api(self, cfg, vm_ip, root, docker_curl, secrets) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        token = secrets.get("GITEA_ADMIN_TOKEN", "")
        if not token:
            return VerifyCheck("gitea", "admin_api", False, "GITEA_ADMIN_TOKEN not set")
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "gitea",
            "http://localhost:3000/api/v1/admin/users",
            root=root,
            headers={"Authorization": f"token {token}"},
        )
        ok = rc == 0 and body and ("login" in body.lower() or "[" in body)
        return VerifyCheck(
            "gitea",
            "admin_api",
            ok,
            "admin user list ok" if ok else (body or "admin API failed")[:120],
        )

    def _check_reverse_proxy_auth(self, cfg, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck, VerifyStatus

        rc, out = docker_exec_on_vm(
            cfg,
            "gitea",
            [
                "/bin/sh",
                "-ec",
                'test "${GITEA__service__ENABLE_REVERSE_PROXY_AUTHENTICATION:-}" = true && printf true',
            ],
            vm_ip,
            root,
            timeout=15,
        )
        if rc != 0:
            return VerifyCheck(
                "gitea",
                "oidc_auth",
                False,
                "reverse-proxy authentication state is not readable",
                status=VerifyStatus.NOT_READY,
            )
        enabled = (out or "").strip().lower() in ("true", "1", "yes")
        return VerifyCheck(
            "gitea",
            "oidc_auth",
            enabled,
            "reverse-proxy authentication enabled" if enabled else "reverse-proxy auth disabled",
        )

    def _check_ssh_port(self, cfg, vm_ip, root, docker_exec_on_vm, ssh_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck, VerifyStatus

        rc, out = docker_exec_on_vm(
            cfg,
            "gitea",
            [
                "/bin/sh",
                "-ec",
                'test "${GITEA__server__DISABLE_SSH:-}" = true && printf true',
            ],
            vm_ip,
            root,
            timeout=15,
        )
        if rc == 0 and (out or "").strip().lower() in ("true", "1", "yes"):
            return VerifyCheck(
                "gitea",
                "ssh_port",
                True,
                "SSH disabled (HTTPS clone only)",
                status=VerifyStatus.NOT_APPLICABLE,
            )
        shell = "nc -z 127.0.0.1 2222 2>/dev/null && echo SSH_OK || nc -z 127.0.0.1 22 2>/dev/null && echo SSH_OK"
        rc2, out2, _ = ssh_on_vm(cfg, vm_ip, shell, root=root, timeout=12)
        ok = rc2 == 0 and "SSH_OK" in (out2 or "")
        return VerifyCheck(
            "gitea",
            "ssh_port",
            ok,
            "SSH clone port reachable" if ok else "SSH port not reachable",
        )
