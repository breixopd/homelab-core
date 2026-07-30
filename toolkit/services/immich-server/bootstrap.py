"""Immich post-deploy bootstrap: admin registration and OIDC configuration."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
from toolkit.core.ops.automation import resolve_docker_service_url
from toolkit.services.sdk import authelia_oidc_issuer, resolve_bootstrap_password

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def mark_immich_geodata_import_complete(secrets: dict[str, str]) -> list[str]:
    """Mark geodata import done so microservices skips the heavy cities500 import on boot."""
    import json
    import subprocess

    from toolkit.core.ops.automation import docker_exec

    password = secrets.get("IMMICH_DB_PASSWORD", "")
    if not password:
        return ["Immich geodata: IMMICH_DB_PASSWORD not set — skip import marker"]

    geodata_paths = (
        "/build/geodata/geodata-date.txt",
        "/usr/src/app/server/dist/resources/geodata/date.txt",
        "/usr/src/app/server/resources/geodata/date.txt",
    )
    geodata_date = ""
    try:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                "ghcr.io/immich-app/immich-server:v2.7.5",
                "/build/geodata/geodata-date.txt",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            geodata_date = proc.stdout.strip().splitlines()[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not geodata_date:
        for path in geodata_paths:
            rc, out = docker_exec("immich-server", ["cat", path], timeout=15)
            if rc == 0 and (out or "").strip():
                geodata_date = out.strip().splitlines()[-1]
                break
    if not geodata_date:
        return ["Immich geodata: could not read geodata date — skip import marker"]

    payload = json.dumps({"lastUpdate": geodata_date, "lastImportFileName": "cities500.txt"})
    secret_environment = {"PGPASSWORD": password}
    upsert = """INSERT INTO system_metadata (key, value)
VALUES ('reverse-geocoding-state', :'payload'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""
    for attempt in range(12):
        rc, out = docker_exec(
            "immich-postgres",
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"payload={payload}",
                "-U",
                "immich",
                "-d",
                "immich",
            ],
            secret_environment=secret_environment,
            stdin=f"{upsert}\n",
            timeout=20,
        )
        if rc == 0:
            return [f"Immich geodata: import marker set (date={geodata_date})"]
        if "recovery mode" not in (out or "").lower():
            return [f"Immich geodata: marker failed ({(out or '')[:80]})"]
        time.sleep(3)
    return [f"Immich geodata: postgres busy ({(out or '')[:80]})"]


def repair_immich_schema_drift(secrets: dict[str, str]) -> list[str]:
    """Apply idempotent geodata schema/index fixes reported by ``immich-admin schema-check``."""
    from toolkit.core.ops.automation import docker_exec

    password = secrets.get("IMMICH_DB_PASSWORD", "")
    if not password:
        return ["Immich schema: IMMICH_DB_PASSWORD not set — skip drift repair"]

    secret_environment = {"PGPASSWORD": password}
    statements = [
        "ALTER TABLE geodata_places ADD CONSTRAINT geodata_places_pkey PRIMARY KEY (id)",
        "ALTER TABLE naturalearth_countries ADD CONSTRAINT naturalearth_countries_pkey PRIMARY KEY (id)",
        (
            "CREATE INDEX IF NOT EXISTS idx_geodata_places_name ON geodata_places "
            "USING gin (f_unaccent(name) gin_trgm_ops)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_geodata_places_admin2_name ON geodata_places "
            'USING gin (f_unaccent("admin2Name") gin_trgm_ops)'
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_geodata_places_admin1_name ON geodata_places "
            'USING gin (f_unaccent("admin1Name") gin_trgm_ops)'
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_geodata_places_alternate_names ON geodata_places "
            'USING gin (f_unaccent("alternateNames") gin_trgm_ops)'
        ),
    ]
    logs: list[str] = []
    for attempt in range(12):
        rc, out = docker_exec(
            "immich-postgres",
            ["psql", "-v", "ON_ERROR_STOP=1", "-U", "immich", "-d", "immich", "-tA"],
            secret_environment=secret_environment,
            stdin="SELECT 1\n",
            timeout=15,
        )
        if rc == 0 and (out or "").strip() == "1":
            break
        time.sleep(5)
    else:
        logs.append(f"Immich schema: postgres not ready ({(out or '')[:80]})")
        return logs

    applied = 0
    for stmt in statements:
        rc, out = docker_exec(
            "immich-postgres",
            ["psql", "-v", "ON_ERROR_STOP=0", "-U", "immich", "-d", "immich"],
            secret_environment=secret_environment,
            stdin=f"{stmt}\n",
            timeout=120,
        )
        if rc == 0:
            applied += 1
        elif "already exists" not in (out or "").lower():
            logs.append(f"Immich schema: drift repair note ({(out or stmt)[:80]})")
    logs.append(f"Immich schema: applied {applied}/{len(statements)} drift fixes")
    return logs


def bootstrap_immich_admin(config: Config, secrets: dict[str, str]) -> list[str]:
    logs: list[str] = []
    base = resolve_docker_service_url("immich-server", 2283)
    email = secrets.get("IMMICH_ADMIN_EMAIL", config.email)
    password = resolve_bootstrap_password(secrets, "IMMICH_ADMIN_PASSWORD")
    if not password:
        logs.append("Immich: SSO_USER_PASSWORD not set — skip admin bootstrap")
        return logs
    ready = False
    for attempt in range(36):
        try:
            ping = httpx.get(f"{base}/api/server/ping", timeout=10)
            if ping.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(5)
    if not ready:
        logs.append("Immich: server not ready after 3min — skip admin bootstrap")
        return logs
    try:
        reg = httpx.post(
            f"{base}/api/auth/admin-sign-up",
            json={"email": email, "password": password, "name": "Admin"},
            timeout=20,
        )
        if reg.status_code in (200, 201):
            logs.append("Immich: admin registered via admin-sign-up")
            admin_exists = False
        elif reg.status_code in (400, 409):
            logs.append("Immich: admin already exists")
            admin_exists = True
        else:
            logs.append(f"Immich: admin bootstrap HTTP {reg.status_code}")
            return logs
        login = httpx.post(
            f"{base}/api/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        )
        if login.status_code in (200, 201):
            logs.append("Immich: admin login verified")
            return logs
        if not admin_exists:
            logs.append(f"Immich: admin login verify HTTP {login.status_code}")
            return logs

        from toolkit.core.ops.automation import docker_exec

        reset_rc, _ = docker_exec(
            "immich-server",
            ["immich-admin", "reset-admin-password"],
            stdin=f"{password}\n",
            timeout=30,
        )
        if reset_rc != 0:
            logs.append("Immich: admin password reconciliation failed")
            return logs
        login = httpx.post(
            f"{base}/api/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        )
        if login.status_code in (200, 201):
            logs.append("Immich: admin password reconciled and login verified")
        else:
            logs.append(f"Immich: reconciled admin login HTTP {login.status_code}")
    except httpx.HTTPError as exc:
        logs.append(f"Immich: admin bootstrap failed ({exc})")
    return logs


def configure_immich_oidc(config: Config, secrets: dict[str, str]) -> list[str]:
    """Enable Immich OAuth login via Authelia OIDC (LLDAP users)."""
    logs: list[str] = []
    if not config.category_enabled("cloud"):
        return logs
    client_secret = secrets.get("IMMICH_OIDC_CLIENT_SECRET", "")
    if not client_secret:
        logs.append("Immich OIDC: IMMICH_OIDC_CLIENT_SECRET missing — skip")
        return logs
    base = resolve_docker_service_url("immich-server", 2283)
    email = secrets.get("IMMICH_ADMIN_EMAIL", config.email)
    password = resolve_bootstrap_password(secrets, "IMMICH_ADMIN_PASSWORD")
    if not password:
        logs.append("Immich OIDC: owner password missing — skip")
        return logs
    issuer = authelia_oidc_issuer(config)
    try:
        login = httpx.post(
            f"{base}/api/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        )
        if login.status_code not in (200, 201):
            logs.append(f"Immich OIDC: admin login HTTP {login.status_code}")
            return logs
        token = login.json().get("accessToken") or login.json().get("access_token")
        if not token:
            logs.append("Immich OIDC: admin login returned no token")
            return logs
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        cfg_resp = httpx.get(f"{base}/api/system-config", headers=headers, timeout=15)
        if cfg_resp.status_code != 200:
            logs.append(f"Immich OIDC: read system-config HTTP {cfg_resp.status_code}")
            return logs
        body = cfg_resp.json() if cfg_resp.content else {}
        oauth = body.get("oauth") if isinstance(body, dict) else {}
        if not isinstance(oauth, dict):
            oauth = {}
        oauth.update(
            {
                "enabled": True,
                "issuerUrl": issuer,
                "clientId": "immich",
                "clientSecret": client_secret,
                "mobileOverrideEnabled": True,
                "mobileRedirectUri": f"https://photos.{config.domain}/api/oauth/mobile-redirect",
                "autoLaunch": False,
                "buttonText": "Login with SSO",
            }
        )
        body["oauth"] = oauth
        patch = httpx.put(f"{base}/api/system-config", headers=headers, json=body, timeout=20)
        if patch.status_code in (200, 201, 204):
            logs.append("Immich OIDC: OAuth enabled via system-config")
        else:
            logs.append(f"Immich OIDC: system-config update HTTP {patch.status_code}")
    except httpx.HTTPError as exc:
        logs.append(f"Immich OIDC: configure failed ({exc})")
    return logs
