"""postgres service plugin.

Custom verify logic for the Postgres service. The base ServicePlugin
defaults (compose_service, env_vars, secrets_needed, credentials) read
from service.yaml; this file overrides only what needs custom Python
logic (verify, post_start, oidc_client, heal).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.ops.dump_repository import DumpRecord
    from toolkit.services.sdk import VerifyCheck


def _psql(
    cfg: Config,
    infra_ip: str,
    root: Path,
    *,
    user: str,
    database: str,
    password: str,
    sql: str,
    timeout: int = 15,
) -> tuple[int, str]:
    """Run a Postgres query without exposing the password or SQL in argv."""
    from toolkit.services.sdk import docker_exec_on_vm

    return docker_exec_on_vm(
        cfg,
        "postgres",
        [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
            "-U",
            user,
            "-d",
            database,
        ],
        infra_ip,
        root,
        timeout=timeout,
        secret_environment={"PGPASSWORD": password},
        stdin=sql.rstrip("\n") + "\n",
    )


class PostgresPlugin(ServicePlugin):
    service = "postgres"
    category = "management"

    def pre_deploy_database_dump(self, cfg: Config, root: Path, *, vm: str | None = None) -> str | None:
        from toolkit.services.postgres.maintenance import pre_deploy_dump

        return pre_deploy_dump(cfg, root, service=self.service, vm=vm)

    def list_database_dumps(self, cfg: Config, root: Path, *, vm: str | None = None) -> list[DumpRecord]:
        from toolkit.services.postgres.maintenance import list_dumps

        return list_dumps(cfg, root, service=self.service, vm=vm)

    def restore_database_dump(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> bool:
        from toolkit.services.postgres.maintenance import restore_dump

        return restore_dump(cfg, root, record, service=self.service, vm=vm)

    def run_database_restore_drill(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> tuple[bool, int, str]:
        from toolkit.services.postgres.maintenance import run_restore_drill

        return run_restore_drill(cfg, root, record, service=self.service, vm=vm)

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Finish first-boot initialization and reconcile application passwords."""
        import time

        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
        from toolkit.core.projects.database import project_database_env_pairs
        from toolkit.services.sdk import (
            ensure_postgres_healthy,
            reconcile_service_databases,
            sync_project_postgres_databases,
        )

        install_root = root or Path(DEFAULT_HOMELAB_ROOT)
        node = self.runtime_node(cfg)
        logs: list[str] = []
        attempt_logs: list[str] = []
        for attempt in range(1, 4):
            attempt_logs = ensure_postgres_healthy(
                install_root,
                node=node,
                env=secrets,
                sync_passwords=False,
            )
            logs.extend(attempt_logs)
            incomplete = any("not ready after" in line or "start failed" in line for line in attempt_logs)
            if not incomplete:
                break
            if attempt < 3:
                logs.append(f"Postgres not ready - retry attempt {attempt + 1}/3 in 10s")
                time.sleep(10)
        if any("not ready after" in line or "start failed" in line for line in attempt_logs):
            raise RuntimeError("Postgres not healthy after 3 bootstrap attempts")

        sync_result = reconcile_service_databases(install_root, node=node, cfg=cfg, env=secrets)
        logs.extend(sync_result.logs)
        if not sync_result.success:
            raise RuntimeError(sync_result.failure_message())
        if project_database_env_pairs(cfg, self.service):
            project_result = sync_project_postgres_databases(
                install_root,
                provider=self.service,
                cfg=cfg,
                env=secrets,
            )
            logs.extend(project_result.logs)
            if not project_result.success:
                raise RuntimeError(project_result.failure_message())
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.core.manifest.databases import compile_database_bindings
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm

        checks: list[VerifyCheck] = []
        if cfg.domain == "localhost":
            return [VerifyCheck("postgres", "connections", True, "skipped (localhost)")]
        runtime_address = self.runtime_address(cfg)
        if not container_exists_on_vm(cfg, runtime_address, "postgres", root):
            return [VerifyCheck("postgres", "connections", False, "container missing")]

        postgres_pass = secrets.get("POSTGRES_PASSWORD", "")
        pg_admin = secrets.get("POSTGRES_USER") or "admin"
        pg_db = secrets.get("POSTGRES_DB") or "postgres"
        if not postgres_pass:
            checks.append(VerifyCheck("postgres", "connections", False, "POSTGRES_PASSWORD not set"))
            return checks

        unreachable = False
        bindings = compile_database_bindings(cfg, provider=self.service)
        for binding in bindings:
            password = secrets.get(binding.password_env, "")
            if not password:
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"user_{binding.username}",
                        False,
                        f"{binding.password_env} not set — cannot verify {binding.username}",
                    )
                )
                continue
            rc, out = _psql(
                cfg,
                runtime_address,
                root,
                user=binding.username,
                database=binding.database,
                password=password,
                sql="SELECT 1",
            )
            if rc == 0 and (out or "").strip() == "1":
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"user_{binding.username}",
                        True,
                        f"{binding.username} can connect to {binding.database}",
                    )
                )
            elif "does not exist" in (out or "").lower():
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"user_{binding.username}",
                        False,
                        f"role {binding.username} or database {binding.database} is missing",
                    )
                )
            elif "connection refused" in (out or "").lower() or "could not connect" in (out or "").lower():
                checks.append(VerifyCheck("postgres", f"user_{binding.username}", False, "postgres not reachable"))
                unreachable = True
                break
            else:
                err = (out or "").strip()[:120]
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"user_{binding.username}",
                        False,
                        err or f"connect failed ({rc})",
                    )
                )

        if unreachable:
            return checks

        users = [binding.username for binding in bindings]
        if users:
            in_list = ", ".join(f"'{u}'" for u in users)
            rc, out = _psql(
                cfg,
                runtime_address,
                root,
                user=pg_admin,
                database=pg_db,
                password=postgres_pass,
                sql=f"SELECT rolname FROM pg_roles WHERE rolname IN ({in_list}) AND NOT rolcanlogin",
            )
            if rc == 0:
                bad = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
                ok = not bad
                detail = "all app users can login" if ok else f"cannot login: {', '.join(bad)}"
                checks.append(VerifyCheck("postgres", "rolcanlogin", ok, detail))

            for binding in bindings:
                rc, out = _psql(
                    cfg,
                    runtime_address,
                    root,
                    user=pg_admin,
                    database=pg_db,
                    password=postgres_pass,
                    sql=(f"SELECT has_database_privilege('{binding.username}', '{binding.database}', 'CONNECT')"),
                )
                ok = rc == 0 and (out or "").strip().lower() == "t"
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"grant_{binding.username}",
                        ok,
                        f"CONNECT on {binding.database}" if ok else (out or "privilege check failed")[:120],
                    )
                )
                rc, out = _psql(
                    cfg,
                    runtime_address,
                    root,
                    user=pg_admin,
                    database=pg_db,
                    password=postgres_pass,
                    sql=(f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{binding.database}'"),
                )
                owner = (out or "").strip()
                checks.append(
                    VerifyCheck(
                        "postgres",
                        f"owner_{binding.username}",
                        rc == 0 and owner == binding.username,
                        (
                            f"{binding.username} owns {binding.database}"
                            if rc == 0 and owner == binding.username
                            else f"owner is {owner or 'unknown'}"
                        ),
                    )
                )

        rc, out = _psql(
            cfg,
            runtime_address,
            root,
            user=pg_admin,
            database=pg_db,
            password=postgres_pass,
            sql="SELECT pg_is_in_recovery()",
        )
        if rc == 0:
            recovering = (out or "").strip().lower() == "t"
            checks.append(
                VerifyCheck(
                    "postgres",
                    "wal_primary",
                    not recovering,
                    "primary (not in recovery)" if not recovering else "standby/in recovery",
                )
            )

        return checks

    def heal(self, cfg: Config, root: Path, *, service: str | None = None) -> list[str] | None:
        from toolkit.services.sdk import ensure_postgres_healthy

        return ensure_postgres_healthy(root, node=self.runtime_node(cfg))
