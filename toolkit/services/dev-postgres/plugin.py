"""dev-postgres service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class DevPostgresPlugin(ServicePlugin):
    service = "dev-postgres"
    category = "cloud"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        from toolkit.services.sdk import ensure_postgres_healthy

        return ensure_postgres_healthy(
            root or Path.cwd(),
            node=self.runtime_node(cfg),
            service="dev-postgres",
            env=secrets,
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.core.config.config import config_path, load_config
        from toolkit.core.projects.database import project_database_env_pairs
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        if not container_exists_on_vm(cfg, vm_ip, "dev-postgres", root):
            return [VerifyCheck("dev-postgres", "ready", True, "dev profile not deployed (skipped)")]

        checks: list[VerifyCheck] = []
        user = secrets.get("DEV_POSTGRES_USER") or "dev"
        admin_pass = secrets.get("DEV_POSTGRES_PASSWORD", "")
        admin_db = secrets.get("DEV_POSTGRES_DB") or "dev"

        rc, out = docker_exec_on_vm(
            cfg,
            "dev-postgres",
            ["pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", user],
            vm_ip,
            root,
            timeout=15,
        )
        ok = rc == 0 and "accepting connections" in (out or "").lower()
        detail = "accepting connections" if ok else (out or "pg_isready failed")[:120]
        checks.append(VerifyCheck("dev-postgres", "ready", ok, detail))
        if not ok:
            return checks

        if admin_pass:
            rc, out = docker_exec_on_vm(
                cfg,
                "dev-postgres",
                ["psql", "-v", "ON_ERROR_STOP=1", "-tA", "-U", user, "-d", admin_db],
                vm_ip,
                root,
                timeout=15,
                secret_environment={"PGPASSWORD": admin_pass},
                stdin="SELECT 1\n",
            )
            admin_ok = rc == 0 and (out or "").strip() == "1"
            checks.append(
                VerifyCheck(
                    "dev-postgres",
                    "admin_connect",
                    admin_ok,
                    f"{user} admin SELECT 1" if admin_ok else (out or "admin connect failed")[:120],
                )
            )

        full_cfg = load_config(config_path(root))
        for pg_user, env_key in project_database_env_pairs(full_cfg, self.service):
            password = secrets.get(env_key, "")
            if not password:
                checks.append(
                    VerifyCheck(
                        "dev-postgres",
                        f"user_{pg_user}",
                        False,
                        f"{env_key} not set — cannot verify {pg_user}",
                    )
                )
                continue
            rc, out = docker_exec_on_vm(
                cfg,
                "dev-postgres",
                ["psql", "-v", "ON_ERROR_STOP=1", "-tA", "-U", pg_user, "-d", pg_user],
                vm_ip,
                root,
                timeout=15,
                secret_environment={"PGPASSWORD": password},
                stdin="SELECT 1\n",
            )
            user_ok = rc == 0 and (out or "").strip() == "1"
            checks.append(
                VerifyCheck(
                    "dev-postgres",
                    f"user_{pg_user}",
                    user_ok,
                    f"{pg_user} can connect" if user_ok else (out or "connect failed")[:120],
                )
            )

        return checks

    def heal(self, cfg: Config, root: Path, *, service: str | None = None) -> list[str] | None:
        from toolkit.services.sdk import ensure_postgres_healthy

        return ensure_postgres_healthy(root, node=self.runtime_node(cfg), service="dev-postgres")
