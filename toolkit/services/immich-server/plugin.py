"""immich-server service plugin.

Owns its verify(), post_start(), and oidc_client on top of the base
ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import OIDCClient, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class ImmichPlugin(ServicePlugin):
    service = "immich-server"
    category = "cloud"

    def prepare_runtime_deployment(self, context, services: tuple[str, ...]) -> None:
        """Create Immich's upload integrity markers before the container starts."""
        if self.service not in services:
            return
        source = context.environment("IMMICH_UPLOAD_SOURCE")
        if not source:
            context.warn("Immich upload source is unset; integrity markers were not created")
            return
        upload = Path(source)
        if not upload.is_absolute():
            upload = context.root / upload
        for name in ("thumbs", "encoded-video", "backups", "library", "profile", "upload"):
            directory = upload / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ".immich").touch(exist_ok=True)
        context.log(f"Immich: ensured upload integrity markers under {upload}")

    @property
    def oidc_client(self) -> OIDCClient:
        return OIDCClient(
            client_id="immich",
            secret_env_var="IMMICH_OIDC_CLIENT_SECRET",
            native=True,
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Immich admin bootstrap + OIDC config."""
        from toolkit.core.ops.automation import health_check_logs, resolve_docker_service_url

        logs: list[str] = []
        logs.extend(
            health_check_logs(
                [
                    ("immich", f"{resolve_docker_service_url('immich-server', 2283)}/api/server/ping"),
                ]
            )
        )
        import importlib
        import subprocess

        bootstrap = importlib.import_module("toolkit.services.immich-server.bootstrap")
        subprocess.run(["docker", "stop", "immich-server"], capture_output=True, text=True, timeout=30, check=False)
        logs.extend(bootstrap.repair_immich_schema_drift(secrets))
        logs.extend(bootstrap.mark_immich_geodata_import_complete(secrets))
        subprocess.run(["docker", "start", "immich-server"], capture_output=True, text=True, timeout=30, check=False)
        logs.extend(bootstrap.bootstrap_immich_admin(cfg, secrets))
        logs.extend(bootstrap.configure_immich_oidc(cfg, secrets))
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Ping, version/DB path, ML reachability, storage writable, OIDC issuer."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            oidc_check_env_issuer,
        )

        if not cfg.category_enabled("cloud"):
            return [VerifyCheck("immich", "ping", True, "cloud not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("immich", "ping", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "immich-server", root):
            return [VerifyCheck("immich", "ping", False, "container missing")]

        checks: list[VerifyCheck] = []
        checks.append(self._check_ping(cfg, vm_ip, root, docker_curl))
        checks.append(self._check_version(cfg, vm_ip, root, docker_curl))
        checks.append(self._check_db_path(cfg, vm_ip, root, docker_curl, secrets))
        checks.append(self._check_ml_reachable(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_storage_writable(cfg, vm_ip, root, docker_exec_on_vm))
        checks.extend(oidc_check_env_issuer(cfg, "immich", "immich-server", "OAUTH_ISSUER_URL", vm_ip, root))
        return checks

    def _check_ping(self, cfg, vm_ip, root, docker_curl) -> VerifyCheck:
        from toolkit.core.ansible.ansible_ssh import sanitize_probe_output
        from toolkit.services.sdk import VerifyCheck

        rc, body = docker_curl(cfg, vm_ip, "immich-server", "http://localhost:2283/api/server/ping", root=root)
        ok = rc == 0 and "pong" in (body or "").lower()
        detail = "pong" if ok else sanitize_probe_output(body or "ping failed")
        return VerifyCheck("immich", "ping", ok, detail)

    def _check_version(self, cfg, vm_ip, root, docker_curl) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, body = docker_curl(cfg, vm_ip, "immich-server", "http://localhost:2283/api/server/version", root=root)
        if rc != 0 or not body:
            return VerifyCheck("immich", "version", False, "version endpoint unreachable")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return VerifyCheck("immich", "version", False, "invalid version JSON")
        version = data.get("major") or data.get("version") or str(data)[:40]
        return VerifyCheck("immich", "version", True, f"version {version}")

    def _check_db_path(self, cfg, vm_ip, root, docker_curl, secrets) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck, VerifyStatus, resolve_bootstrap_password

        email = secrets.get("IMMICH_ADMIN_EMAIL") or cfg.email
        password = resolve_bootstrap_password(secrets, "IMMICH_ADMIN_PASSWORD")
        if not email or not password:
            return VerifyCheck(
                "immich",
                "db_migrations",
                False,
                "admin credentials unavailable for /users/me",
                status=VerifyStatus.NOT_READY,
            )

        from toolkit.services.sdk import docker_exec_on_vm

        # Keep the command static: credentials are expanded inside the
        # container from the stdin-backed secret environment wrapper, never
        # serialized into Docker/SSH argv or emitted in diagnostics.
        login_command = [
            "sh",
            "-c",
            "node -e 'process.stdout.write(JSON.stringify({"
            "email:process.env.HOMELAB_VERIFY_EMAIL,password:process.env.HOMELAB_VERIFY_PASSWORD}))' "
            "| curl -sf --max-time 12 -X POST http://localhost:2283/api/auth/login "
            "-H 'Content-Type: application/json' --data-binary @-",
        ]
        login_rc, login_body = 1, ""
        for _attempt in range(5):
            ping_rc, ping_body = docker_curl(
                cfg,
                vm_ip,
                "immich-server",
                "http://localhost:2283/api/server/ping",
                root=root,
            )
            if ping_rc != 0 or "pong" not in (ping_body or "").lower():
                time.sleep(3)
                continue
            login_rc, login_body = docker_exec_on_vm(
                cfg,
                "immich-server",
                login_command,
                vm_ip,
                root,
                timeout=20,
                secret_environment={
                    "HOMELAB_VERIFY_EMAIL": email,
                    "HOMELAB_VERIFY_PASSWORD": password,
                },
            )
            if login_rc == 0 and login_body:
                break
            time.sleep(3)
        if login_rc != 0 or not login_body:
            return VerifyCheck("immich", "db_migrations", False, "admin login failed (DB path blocked)")
        try:
            login_data = json.loads(login_body)
            token = login_data.get("accessToken") or login_data.get("access_token")
        except json.JSONDecodeError:
            return VerifyCheck("immich", "db_migrations", False, "login response invalid")
        if not token:
            return VerifyCheck("immich", "db_migrations", False, "login returned no token")
        me_rc, me_body = docker_curl(
            cfg,
            vm_ip,
            "immich-server",
            "http://localhost:2283/api/users/me",
            root=root,
            headers={"Authorization": f"Bearer {token}"},
        )
        ok = me_rc == 0 and "email" in (me_body or "").lower()
        return VerifyCheck(
            "immich",
            "db_migrations",
            ok,
            "DB path ok (/users/me)" if ok else "users/me failed",
        )

    def _check_ml_reachable(self, cfg, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm

        if not container_exists_on_vm(cfg, vm_ip, "immich-machine-learning", root):
            return VerifyCheck("immich", "ml_ping", False, "ML container missing")
        rc, out = docker_exec_on_vm(
            cfg,
            "immich-server",
            ["sh", "-c", "curl -sf --max-time 8 http://immich-machine-learning:3003/ping"],
            vm_ip,
            root,
            timeout=15,
        )
        ok = rc == 0
        return VerifyCheck("immich", "ml_ping", ok, "ML /ping ok" if ok else (out or "ML unreachable")[:120])

    def _check_storage_writable(self, cfg, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, out = docker_exec_on_vm(
            cfg,
            "immich-server",
            [
                "sh",
                "-c",
                "test -w /usr/src/app/upload && "
                "touch /usr/src/app/upload/.verify_write && "
                "rm -f /usr/src/app/upload/.verify_write && echo OK",
            ],
            vm_ip,
            root,
            timeout=15,
        )
        ok = rc == 0 and "OK" in (out or "")
        return VerifyCheck(
            "immich",
            "storage_writable",
            ok,
            "upload path writable" if ok else (out or "upload path not writable")[:120],
        )
