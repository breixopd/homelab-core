"""Authelia-owned storage and directory recovery helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
from toolkit.core.ops.automation import docker_exec
from toolkit.services.sdk.postgres import load_env_file, sql_literal


def reset_authelia_storage(root: Path, *, docker_bin: str = "docker") -> list[str]:
    """Drop and recreate Authelia storage when encryption-key drift blocks startup."""
    from toolkit.core.config.config import load_config
    from toolkit.core.manifest.placement import service_node

    logs: list[str] = []
    config = load_config(root / "config.yaml")
    env_path = root / "generated" / service_node(config, "postgres") / ".env"
    merged = load_env_file(env_path)
    pg_user = merged.get("POSTGRES_USER") or "admin"
    pg_pass = merged.get("POSTGRES_PASSWORD", "")
    authelia_pass = merged.get("AUTHELIA_DB_PASSWORD", "")

    if not pg_pass or not authelia_pass:
        logs.append("Authelia: missing postgres/authelia passwords - skip storage reset")
        return logs

    steps = [
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'authelia' AND pid <> pg_backend_pid();",
        "DROP DATABASE IF EXISTS authelia;",
        (
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'authelia') "
            f"THEN CREATE USER authelia WITH PASSWORD {sql_literal(authelia_pass)}; "
            f"ELSE ALTER USER authelia WITH PASSWORD {sql_literal(authelia_pass)}; END IF; END $$;"
        ),
        "CREATE DATABASE authelia OWNER authelia;",
    ]
    for sql in steps:
        rc, out = docker_exec(
            "postgres",
            ["psql", "-v", "ON_ERROR_STOP=1", "-U", pg_user, "-d", "postgres"],
            secret_environment={"PGPASSWORD": pg_pass},
            stdin=f"{sql}\n",
            timeout=60,
            docker_bin=docker_bin,
        )
        if rc != 0 and "already exists" not in (out or "").lower():
            logs.append(f"Authelia: storage reset failed ({(out or '')[:120]})")
            return logs

    logs.append("Authelia: recreated authelia database (encryption key resync)")
    subprocess.run([docker_bin, "restart", "authelia"], capture_output=True, timeout=60, check=False)
    logs.append("Authelia: container restarted")
    return logs


def heal_authelia(root: Path | None = None, *, docker_bin: str = "docker") -> list[str]:
    """Heal Authelia restart loops without replacing unrelated service state."""
    root = Path(root or DEFAULT_HOMELAB_ROOT)
    proc = subprocess.run(
        [docker_bin, "logs", "--tail", "40", "authelia"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    logs_text = proc.stdout + proc.stderr
    if "encryption key does not appear to be valid" in logs_text:
        return reset_authelia_storage(root, docker_bin=docker_bin)
    if "Invalid Credentials" in logs_text or "LDAP Result Code 49" in logs_text:
        from toolkit.core.config.config import config_path, load_config
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.identity.lldap_client import LLDAPClient
        from toolkit.core.secrets.secrets import load_secrets_plaintext

        cfg = load_config(config_path(root))
        secrets = load_secrets_plaintext(secrets_path(root))
        bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
        admin_password = secrets.get("LLDAP_ADMIN_PASSWORD", "")
        if bind_password and admin_password:
            try:
                client = LLDAPClient(admin_password=admin_password, root=root)
                lines = client.ensure_service_bind(bind_password, domain=cfg.domain or "")
                output = ["Authelia: synced ldap-bind after LDAP auth failure", *[f"LLDAP: {line}" for line in lines]]
                subprocess.run([docker_bin, "restart", "authelia"], capture_output=True, timeout=60, check=False)
                output.append("Authelia: container restarted after ldap-bind sync")
                return output
            except RuntimeError as exc:
                return [f"Authelia: ldap-bind heal failed ({exc})"]
    return ["Authelia: no storage heal needed"]
