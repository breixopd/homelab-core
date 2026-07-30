"""immich-postgres service plugin.

Owns its verify() check on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class ImmichPostgresPlugin(ServicePlugin):
    service = "immich-postgres"
    category = "cloud"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Ensure extensions required by the pinned Immich image exist.

        The upstream image runs migrations that assume both extensions are
        available.  Running this through the existing post-start lifecycle is
        idempotent and also repairs databases created before the extensions
        were added.  The administrator password is passed only through the
        container environment, never in argv or captured SQL.
        """
        from toolkit.core.ops.automation import docker_exec

        password = secrets.get("IMMICH_POSTGRES_ADMIN_PASSWORD", "")
        if not password:
            return ["Hook error: Immich PostgreSQL admin password is missing"]
        sql = "CREATE EXTENSION IF NOT EXISTS vchord;\nCREATE EXTENSION IF NOT EXISTS earthdistance;\n"
        command = [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            secrets.get("IMMICH_POSTGRES_ADMIN_USER", "postgres"),
            "-d",
            "immich",
        ]
        last_output = ""
        for attempt in range(5):
            rc, output = docker_exec(
                "immich-postgres",
                command,
                secret_environment={"PGPASSWORD": password},
                stdin=sql,
                timeout=30,
            )
            if rc == 0:
                return ["Immich PostgreSQL: vchord and earthdistance extensions ready"]
            last_output = output or ""
            if "recovery mode" not in last_output.lower() and "could not connect" not in last_output.lower():
                break
            time.sleep(3)
        return [f"Immich PostgreSQL: extension bootstrap failed ({last_output[:100]})"]

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Required extensions, Immich database, and application connectivity."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        if not cfg.category_enabled("cloud"):
            return [VerifyCheck("immich-postgres", "extensions", True, "cloud not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("immich-postgres", "extensions", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "immich-postgres", root):
            return [VerifyCheck("immich-postgres", "extensions", False, "container missing")]

        password = secrets.get("IMMICH_DB_PASSWORD", "")
        if not password:
            return [VerifyCheck("immich-postgres", "extensions", False, "IMMICH_DB_PASSWORD not set")]

        return [
            self._psql_check(
                cfg,
                vm_ip,
                root,
                docker_exec_on_vm,
                password,
                "SELECT 1",
                "connect",
                "immich user connect ok",
            ),
            self._psql_check(
                cfg,
                vm_ip,
                root,
                docker_exec_on_vm,
                password,
                "SELECT 1 FROM pg_database WHERE datname='immich'",
                "database",
                "immich database exists",
            ),
            self._extension_check(cfg, vm_ip, root, docker_exec_on_vm, password),
        ]

    def _psql_check(
        self,
        cfg,
        vm_ip,
        root,
        docker_exec_on_vm,
        password: str,
        sql: str,
        check: str,
        ok_detail: str,
    ) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, out = self._run_psql(cfg, vm_ip, root, docker_exec_on_vm, password, sql, timeout=20)
        ok = rc == 0 and (out or "").strip() == "1"
        return VerifyCheck(
            "immich-postgres",
            check,
            ok,
            ok_detail if ok else (out or "psql failed")[:120],
        )

    def _run_psql(
        self,
        cfg,
        vm_ip,
        root,
        docker_exec_on_vm,
        password: str,
        sql: str,
        *,
        timeout: int,
    ) -> tuple[int, str]:
        cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-U", "immich", "-d", "immich", "-tAc", sql]
        for attempt in range(3):
            rc, out = docker_exec_on_vm(
                cfg,
                "immich-postgres",
                cmd,
                vm_ip,
                root,
                timeout=timeout,
                secret_environment={"PGPASSWORD": password},
            )
            if rc == 0 or "recovery mode" not in (out or "").lower():
                return rc, out
            time.sleep(3)
        return rc, out

    def _extension_check(self, cfg, vm_ip, root, docker_exec_on_vm, password) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        sql = "SELECT extname FROM pg_extension WHERE extname IN ('vector','vchord','earthdistance');"
        rc, out = self._run_psql(cfg, vm_ip, root, docker_exec_on_vm, password, sql, timeout=20)
        if rc != 0:
            return VerifyCheck("immich-postgres", "extensions", False, (out or "psql failed")[:120])
        extensions = {line.strip() for line in (out or "").splitlines() if line.strip()}
        required = {"vchord", "earthdistance"}
        ok = required.issubset(extensions)
        detail = f"extensions: {', '.join(sorted(extensions)) or 'none'}"
        return VerifyCheck("immich-postgres", "extensions", ok, detail)
