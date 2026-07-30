"""nextcloud service plugin.

Owns its verify(), post_start(), and oidc_client on top of the base
ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import IdentityProvisionResult, OIDCClient, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class NextcloudPlugin(ServicePlugin):
    service = "nextcloud"
    category = "cloud"

    @property
    def oidc_client(self) -> OIDCClient:
        return OIDCClient(
            client_id="nextcloud",
            redirect_uris=[],
            secret_env_var="NEXTCLOUD_OIDC_CLIENT_SECRET",
            native=False,
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Run Nextcloud admin bootstrap + trusted domain + OIDC config."""
        import importlib

        bootstrap = importlib.import_module("toolkit.services.nextcloud.bootstrap")
        bootstrap_nextcloud_admin = bootstrap.bootstrap_nextcloud_admin
        configure_nextcloud_oidc = bootstrap.configure_nextcloud_oidc
        configure_nextcloud_trusted_domain = bootstrap.configure_nextcloud_trusted_domain
        from toolkit.core.ops.automation import resolve_docker_service_url

        logs: list[str] = []
        domain = cfg.domain
        cloud_host = f"cloud.{domain}"

        from toolkit.core.ops.automation import health_check_logs

        logs.extend(
            health_check_logs(
                [
                    ("nextcloud", f"{resolve_docker_service_url('nextcloud', 80)}/status.php"),
                ]
            )
        )

        admin_logs = bootstrap_nextcloud_admin(cfg, secrets)
        logs.extend(admin_logs)
        # The bootstrap helpers have bounded readiness waits. Do not invoke
        # the same five-minute wait again for each follow-up operation when
        # Nextcloud is still starting or its database is unavailable.
        if any(marker in line.lower() for line in admin_logs for marker in ("occ not ready", "install failed")):
            return logs

        trusted_domain_logs = configure_nextcloud_trusted_domain(cloud_host)
        logs.extend(trusted_domain_logs)
        if any("occ not ready" in line.lower() for line in trusted_domain_logs):
            return logs

        oidc_logs = configure_nextcloud_oidc(cfg, secrets)
        logs.extend(oidc_logs)
        if any("occ not ready" in line.lower() for line in oidc_logs):
            return logs
        logs.extend(result.message for result in self.provision_identity(cfg, secrets, cfg.email, root=root))
        logs.extend(bootstrap.configure_nextcloud_background_jobs())
        return logs

    def provision_identity(
        self,
        cfg: Config,
        secrets: dict[str, str],
        email: str,
        *,
        root: Path | None = None,
    ) -> tuple[IdentityProvisionResult, ...]:
        """Enable OIDC user and group provisioning through Nextcloud OCC."""
        from toolkit.core.compose.registry import get_category, load_all
        from toolkit.core.ops.automation import docker_exec

        load_all()
        cloud_group = get_category(self.category).service_group
        commands = (
            (
                ["php", "occ", "config:app:set", "oidc_login", "auto_provision", "--value", "1"],
                "nextcloud_oidc_auto_provision",
                "auto_provision",
            ),
            (
                [
                    "php",
                    "occ",
                    "config:app:set",
                    "oidc_login",
                    "oidc_provision_groups",
                    "--value",
                    cloud_group,
                ],
                "nextcloud_oidc_group_provision",
                "provision group",
            ),
        )
        results: list[IdentityProvisionResult] = []
        for command, key, label in commands:
            if cfg.is_multi_node:
                from toolkit.core.manifest.placement import service_address
                from toolkit.services.sdk import docker_exec_on_vm

                rc, output = docker_exec_on_vm(
                    cfg,
                    self.service,
                    command,
                    service_address(cfg, self.service),
                    (root or Path.cwd()).resolve(),
                    user="www-data",
                )
            else:
                rc, output = docker_exec(self.service, command, user="www-data")
            results.append(
                IdentityProvisionResult(
                    key,
                    "completed" if rc == 0 else "failed",
                    (
                        f"Nextcloud OIDC: {label} OK"
                        if rc == 0
                        else f"Nextcloud OIDC: {label} failed ({(output or '')[:80]})"
                    ),
                )
            )
        return tuple(results)

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Nextcloud status, DB/redis, OIDC provider, and cron background jobs."""
        from toolkit.services.sdk import (
            VerifyCheck,
            authelia_oidc_issuer,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
        )

        if not cfg.category_enabled("cloud"):
            return [VerifyCheck("nextcloud", "status", True, "cloud not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("nextcloud", "status", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "nextcloud", root):
            return [VerifyCheck("nextcloud", "status", False, "container missing")]

        checks: list[VerifyCheck] = []
        checks.append(self._check_status_php(cfg, vm_ip, root, docker_curl))
        checks.extend(self._check_occ_status(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_db_redis(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_oidc_provider(cfg, vm_ip, root, docker_exec_on_vm, authelia_oidc_issuer))
        checks.extend(self._check_background_jobs(cfg, vm_ip, root, docker_exec_on_vm))
        return checks

    def _occ(self, cfg, vm_ip, root, docker_exec_on_vm, args: list[str]) -> tuple[int, str]:
        return docker_exec_on_vm(cfg, "nextcloud", ["php", "occ", *args], vm_ip, root, user="www-data", timeout=20)

    def _check_status_php(self, cfg, vm_ip, root, docker_curl) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, body = docker_curl(cfg, vm_ip, "nextcloud", "http://localhost/status.php", root=root, timeout=12)
        if rc != 0 or not body:
            return VerifyCheck("nextcloud", "status", False, "status.php unreachable")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return VerifyCheck("nextcloud", "status", False, "status.php invalid JSON")
        ok = bool(data.get("installed")) and not bool(data.get("maintenance"))
        detail = f"installed={data.get('installed')} maintenance={data.get('maintenance')}"
        return VerifyCheck("nextcloud", "status", ok, detail)

    def _check_occ_status(self, cfg, vm_ip, root, docker_exec_on_vm) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck

        rc, out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["status"])
        if rc != 0:
            return [VerifyCheck("nextcloud", "occ_status", False, (out or "occ status failed")[:120])]
        installed = "installed: true" in out
        maintenance = "maintenance: true" in out
        needs_upgrade = "needsDbUpgrade: true" in out
        ok = installed and not maintenance and not needs_upgrade
        detail = "occ status ok" if ok else (out.strip()[:120] or "occ status failed")
        return [VerifyCheck("nextcloud", "occ_status", ok, detail)]

    def _check_db_redis(self, cfg, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["status"])
        if rc != 0:
            return VerifyCheck("nextcloud", "db_redis", False, (out or "occ status failed")[:120])
        db_ok = "installed: true" in out and "maintenance: false" in out and "needsDbUpgrade: false" in out
        if not db_ok:
            return VerifyCheck("nextcloud", "db_redis", False, (out.strip()[:120] or "db check failed"))
        redis_rc, redis_out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["redis:command", "PING"])
        if redis_rc == 0 and "PONG" in (redis_out or "").upper():
            return VerifyCheck("nextcloud", "db_redis", True, "db ok + redis PONG")
        if redis_rc != 0:
            return VerifyCheck("nextcloud", "db_redis", True, "db ok (redis occ skipped)")
        return VerifyCheck("nextcloud", "db_redis", False, (redis_out or "redis PING failed")[:120])

    def _check_oidc_provider(self, cfg, vm_ip, root, docker_exec_on_vm, authelia_oidc_issuer) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        expected = authelia_oidc_issuer(cfg)
        rc, out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["user_oidc:provider"])
        if rc != 0:
            rc2, url_out = self._occ(
                cfg,
                vm_ip,
                root,
                docker_exec_on_vm,
                ["config:app:get", "oidc_login", "oidc_provider_url"],
            )
            if rc2 != 0:
                return VerifyCheck("nextcloud", "oidc_provider", False, "could not read OIDC config")
            url = url_out.strip().splitlines()[-1] if url_out.strip() else ""
            match = url == expected
            return VerifyCheck("nextcloud", "oidc_provider", match, url if match else f"{url} (expected {expected})")
        ok = bool(out.strip()) and ("authelia" in out.lower() or expected.split("://", 1)[-1] in out)
        return VerifyCheck(
            "nextcloud",
            "oidc_provider",
            ok,
            "OIDC provider registered" if ok else (out or "no OIDC provider")[:120],
        )

    def _check_background_jobs(self, cfg, vm_ip, root, docker_exec_on_vm) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck

        checks: list[VerifyCheck] = []
        rc, mode_out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["config:system:get", "backgroundjobs_mode"])
        mode = (mode_out or "").strip().splitlines()[-1] if rc == 0 else ""
        cron_mode = mode == "cron"
        checks.append(
            VerifyCheck(
                "nextcloud",
                "cron_mode",
                cron_mode,
                f"backgroundjobs_mode={mode or 'unknown'}",
            )
        )
        rc2, last_out = self._occ(cfg, vm_ip, root, docker_exec_on_vm, ["config:app:get", "core", "lastcron"])
        if rc2 != 0 or not (last_out or "").strip():
            checks.append(VerifyCheck("nextcloud", "last_cron", False, "lastcron unavailable"))
            return checks
        try:
            last_ts = int((last_out or "").strip().splitlines()[-1])
        except ValueError:
            checks.append(VerifyCheck("nextcloud", "last_cron", False, f"lastcron unreadable: {last_out[:40]}"))
            return checks
        age_s = int(time.time()) - last_ts
        ok = age_s < 900
        checks.append(
            VerifyCheck(
                "nextcloud",
                "last_cron",
                ok,
                f"last cron {age_s // 60}m ago" + ("" if ok else " (>15m)"),
            )
        )
        return checks
