"""Reconcile manifest and project databases on PostgreSQL providers."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from toolkit.core.ops.automation import docker_exec

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog

ReconcileStatus = Literal["ok", "failed"]


@dataclass
class PostgresReconcileResult:
    """Per-role outcome of PostgreSQL database reconciliation."""

    logs: list[str] = field(default_factory=list)
    roles: dict[str, ReconcileStatus] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return not any(status == "failed" for status in self.roles.values())

    @property
    def failed_roles(self) -> list[str]:
        return [role for role, status in self.roles.items() if status == "failed"]

    def failure_message(self) -> str:
        failed = ", ".join(self.failed_roles) or "unknown"
        return f"Postgres: database reconciliation FAILED — roles: {failed}"


@dataclass(frozen=True, slots=True)
class PsqlCommandResult:
    """Small subprocess-compatible result for an in-container psql request."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def load_env_file(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def sql_literal(password: str) -> str:
    return "'" + password.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    if not value or "\x00" in value or len(value.encode("utf-8")) > 63:
        raise ValueError("Postgres identifier must be 1-63 bytes and contain no NUL")
    return '"' + value.replace('"', '""') + '"'


def _run_psql(
    *,
    docker_bin: str,
    pg_container: str,
    pg_pass: str,
    pg_user: str,
    pg_db: str,
    sql: str,
    timeout: int = 30,
) -> PsqlCommandResult:
    """Run SQL through psql without exposing credentials or SQL in argv."""
    rc, output = docker_exec(
        pg_container,
        ["psql", "-v", "ON_ERROR_STOP=1", "-U", pg_user, "-d", pg_db],
        secret_environment={"PGPASSWORD": pg_pass},
        stdin=sql.rstrip("\n") + "\n",
        timeout=timeout,
        docker_bin=docker_bin,
    )
    if rc == 0:
        return PsqlCommandResult(returncode=0, stdout=output)
    return PsqlCommandResult(returncode=rc, stderr=output)


def _sync_one_user(
    *,
    docker_bin: str,
    pg_container: str,
    pg_pass: str,
    pg_user: str,
    pg_db: str,
    user: str,
    database: str,
    password: str,
    log_prefix: str,
) -> tuple[ReconcileStatus, list[str]]:
    logs: list[str] = []
    identifier = _sql_identifier(user)
    sql = (
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = {sql_literal(user)}) "
        f"THEN CREATE USER {identifier} WITH PASSWORD {sql_literal(password)}; "
        f"ELSE ALTER USER {identifier} WITH PASSWORD {sql_literal(password)}; END IF; END $$;"
    )
    try:
        proc = _run_psql(
            docker_bin=docker_bin,
            pg_container=pg_container,
            pg_pass=pg_pass,
            pg_user=pg_user,
            pg_db=pg_db,
            sql=sql,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logs.append(f"{log_prefix}: {user} sync failed ({exc})")
        return "failed", logs

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "does not exist" in err.lower():
            logs.append(f"{log_prefix}: role {user} reconciliation failed ({err[:120]})")
        else:
            logs.append(f"{log_prefix}: {user} sync failed ({err[:120]})")
        return "failed", logs

    database_identifier = _sql_identifier(database)
    try:
        db_proc = _run_psql(
            docker_bin=docker_bin,
            pg_container=pg_container,
            pg_pass=pg_pass,
            pg_user=pg_user,
            pg_db=pg_db,
            sql=f"CREATE DATABASE {database_identifier} OWNER {identifier}",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logs.append(f"{log_prefix}: {database} create failed ({exc})")
        return "failed", logs
    if db_proc.returncode != 0 and "already exists" not in (db_proc.stderr or "").lower():
        err = (db_proc.stderr or db_proc.stdout or "database creation failed").strip()
        logs.append(f"{log_prefix}: database {database} create failed ({err[:120]})")
        return "failed", logs
    try:
        ownership_proc = _run_psql(
            docker_bin=docker_bin,
            pg_container=pg_container,
            pg_pass=pg_pass,
            pg_user=pg_user,
            pg_db=pg_db,
            sql=(
                f"ALTER DATABASE {database_identifier} OWNER TO {identifier}; "
                f"GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {database_identifier} TO {identifier}"
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logs.append(f"{log_prefix}: {database} ownership sync failed ({exc})")
        return "failed", logs
    if ownership_proc.returncode != 0:
        err = (ownership_proc.stderr or ownership_proc.stdout or "ownership reconciliation failed").strip()
        logs.append(f"{log_prefix}: {database} ownership sync failed ({err[:120]})")
        return "failed", logs
    logs.append(f"{log_prefix}: role {user} and database {database} reconciled")
    return "ok", logs


def reconcile_service_databases(
    root: Path,
    *,
    node: str | None = None,
    provider: str = "postgres",
    cfg: Config | None = None,
    catalog: ServiceCatalog | None = None,
    env: dict[str, str] | None = None,
    docker_bin: str = "docker",
) -> PostgresReconcileResult:
    """Reconcile manifest-owned application databases on a PostgreSQL provider."""
    result = PostgresReconcileResult()
    if cfg is None:
        from toolkit.core.config.config import Config, load_config

        config_file = root / "config.yaml"
        cfg = load_config(config_file) if config_file.is_file() else Config()
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.databases import compile_database_bindings

    selected = catalog or load_service_catalog(root if (root / "toolkit" / "services").is_dir() else None)
    provider_manifest = selected.require(provider)
    contract = provider_manifest.database_provider
    if contract is None or contract.engine != "postgresql":
        raise ValueError(f"service {provider!r} is not a PostgreSQL database provider")
    if node is None:
        from toolkit.core.manifest.placement import service_node

        node = service_node(cfg, provider)
    env_path = root / "generated" / node / ".env"
    merged = load_env_file(env_path)
    if env:
        merged.update(env)

    endpoint = provider_manifest.service_endpoint
    if endpoint is None:
        raise ValueError(f"database provider {provider!r} does not declare a service endpoint")
    pg_container = endpoint.compose_service or provider
    pg_user = merged.get(contract.admin_username_env) or provider_manifest.variables.get(
        contract.admin_username_env, "admin"
    )
    pg_db = merged.get(contract.admin_database_env) or provider_manifest.variables.get(
        contract.admin_database_env, "postgres"
    )
    pg_pass = merged.get(contract.admin_password_env, "")
    bindings = compile_database_bindings(cfg, selected, provider=provider)
    if not pg_pass:
        result.logs.append(f"Postgres: {contract.admin_password_env} missing — database reconciliation failed")
        for binding in bindings:
            result.roles[binding.username] = "failed"
        return result

    updated = 0
    for binding in bindings:
        password = merged.get(binding.password_env, "")
        if not password:
            result.roles[binding.username] = "failed"
            result.logs.append(f"Postgres: {binding.password_env} not set — cannot reconcile {binding.username}")
            continue

        status, user_logs = _sync_one_user(
            docker_bin=docker_bin,
            pg_container=pg_container,
            pg_pass=pg_pass,
            pg_user=pg_user,
            pg_db=pg_db,
            user=binding.username,
            database=binding.database,
            password=password,
            log_prefix="Postgres",
        )
        result.roles[binding.username] = status
        result.logs.extend(user_logs)
        if status == "ok":
            updated += 1

    expected = sum(1 for binding in bindings if merged.get(binding.password_env, ""))
    result.logs.append(f"Postgres: reconciled {updated}/{expected} service databases")
    if not result.success:
        result.logs.append(result.failure_message())
    return result


def _pg_is_ready(container: str, user: str, *, docker_bin: str = "docker") -> bool:
    try:
        proc = subprocess.run(
            [docker_bin, "exec", container, "pg_isready", "-U", user],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def ensure_postgres_healthy(
    root: Path,
    *,
    node: str | None = None,
    service: str = "postgres",
    cfg: Config | None = None,
    catalog: ServiceCatalog | None = None,
    env: dict[str, str] | None = None,
    docker_bin: str = "docker",
    sync_passwords: bool = True,
) -> list[str]:
    """Start Postgres, wait for readiness, and optionally sync app passwords."""
    logs: list[str] = []
    if cfg is None:
        from toolkit.core.config.config import Config, load_config

        config_file = root / "config.yaml"
        cfg = load_config(config_file) if config_file.is_file() else Config()
    from toolkit.core.manifest.catalog import load_service_catalog

    selected = catalog or load_service_catalog(root if (root / "toolkit" / "services").is_dir() else None)
    provider_manifest = selected.require(service)
    contract = provider_manifest.database_provider
    if contract is None or contract.engine != "postgresql":
        raise ValueError(f"service {service!r} is not a PostgreSQL database provider")
    if node is None:
        from toolkit.core.manifest.placement import service_node

        node = service_node(cfg, service)
    env_path = root / "generated" / node / ".env"
    merged = load_env_file(env_path)
    if env:
        merged.update(env)

    endpoint = provider_manifest.service_endpoint
    if endpoint is None:
        raise ValueError(f"database provider {service!r} does not declare a service endpoint")
    compose_service = endpoint.compose_service or service
    pg_container = compose_service
    pg_user = merged.get(contract.admin_username_env) or provider_manifest.variables.get(
        contract.admin_username_env, "admin"
    )

    try:
        state = subprocess.run(
            [docker_bin, "inspect", "--format", "{{.State.Status}}", pg_container],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logs.append(f"Postgres: inspect failed ({exc})")
        return logs
    status = state.stdout.strip()

    if status != "running":
        logs.append(f"Postgres: {pg_container} is {status or 'missing'} — starting")
        try:
            up = subprocess.run(
                [
                    docker_bin,
                    "compose",
                    "--env-file",
                    str(env_path),
                    "up",
                    "-d",
                    compose_service,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                cwd=str(root),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logs.append(f"Postgres: start failed ({exc})")
            return logs
        if up.returncode != 0:
            logs.append(f"Postgres: start failed ({(up.stderr or up.stdout or '')[:120]})")
            return logs

    for attempt in range(20):
        if _pg_is_ready(pg_container, pg_user, docker_bin=docker_bin):
            logs.append(f"Postgres: {pg_container} accepting connections")
            break
        time.sleep(3)
    else:
        logs.append(f"Postgres: {pg_container} not ready after 60s")
        return logs

    if sync_passwords:
        from toolkit.core.manifest.databases import compile_database_bindings
        from toolkit.core.projects.database import project_database_env_pairs

        if compile_database_bindings(cfg, selected, provider=service):
            sync_result = reconcile_service_databases(
                root,
                node=node,
                provider=service,
                cfg=cfg,
                catalog=selected,
                env=env,
                docker_bin=docker_bin,
            )
            logs.extend(sync_result.logs)
            if not sync_result.success:
                raise RuntimeError(sync_result.failure_message())
        if project_database_env_pairs(cfg, service):
            project_result = sync_project_postgres_databases(
                root,
                provider=service,
                cfg=cfg,
                catalog=selected,
                env=env,
                docker_bin=docker_bin,
            )
            logs.extend(project_result.logs)
            if not project_result.success:
                raise RuntimeError(project_result.failure_message())
    return logs


def sync_project_postgres_databases(
    root: Path,
    *,
    provider: str,
    cfg: Config | None = None,
    catalog: ServiceCatalog | None = None,
    env: dict[str, str] | None = None,
    docker_bin: str = "docker",
) -> PostgresReconcileResult:
    """Synchronize managed-project tenants on one PostgreSQL provider."""
    result = PostgresReconcileResult()
    if cfg is None:
        from toolkit.core.config.config import Config, load_config

        config_file = root / "config.yaml"
        cfg = load_config(config_file) if config_file.is_file() else Config()
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_node

    selected = catalog or load_service_catalog(root if (root / "toolkit" / "services").is_dir() else None)
    provider_manifest = selected.require(provider)
    contract = provider_manifest.database_provider
    if contract is None or contract.engine != "postgresql":
        raise ValueError(f"service {provider!r} is not a PostgreSQL database provider")
    env_path = root / "generated" / service_node(cfg, provider) / ".env"
    merged = load_env_file(env_path)
    if env:
        merged.update(env)

    endpoint = provider_manifest.service_endpoint
    if endpoint is None:
        raise ValueError(f"database provider {provider!r} does not declare a service endpoint")
    pg_container = endpoint.compose_service or provider
    pg_user = merged.get(contract.admin_username_env) or provider_manifest.variables.get(
        contract.admin_username_env, "admin"
    )
    pg_pass = merged.get(contract.admin_password_env, "")
    pg_db = merged.get(contract.admin_database_env) or provider_manifest.variables.get(
        contract.admin_database_env, "postgres"
    )
    # Managed projects owned by this provider are declared in config.yaml.
    from toolkit.core.projects.database import project_database_env_pairs

    dev_app_users = project_database_env_pairs(cfg, provider)
    if not dev_app_users:
        result.logs.append(f"{provider}: no project tenants configured — skip database reconciliation")
        return result
    if not pg_pass:
        result.logs.append(f"{provider}: {contract.admin_password_env} missing — project reconciliation failed")
        result.roles.update({user: "failed" for user, _env_key in dev_app_users})
        return result

    updated = 0
    for user, env_key in dev_app_users:
        password = merged.get(env_key, "")
        if not password:
            result.roles[user] = "failed"
            result.logs.append(f"{provider}: {env_key} not set — cannot reconcile {user}")
            continue

        status, user_logs = _sync_one_user(
            docker_bin=docker_bin,
            pg_container=pg_container,
            pg_pass=pg_pass,
            pg_user=pg_user,
            pg_db=pg_db,
            user=user,
            database=user,
            password=password,
            log_prefix=provider,
        )
        result.roles[user] = status
        result.logs.extend(user_logs)
        if status == "ok":
            updated += 1

    result.logs.append(f"{provider}: synced project databases for {updated}/{len(dev_app_users)} tenants")
    if not result.success:
        result.logs.append(result.failure_message().replace("Postgres:", f"{provider}:", 1))
    return result
