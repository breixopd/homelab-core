"""Nextcloud post-deploy bootstrap: occ install and admin user verification."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from toolkit.core.ops.automation import docker_exec
from toolkit.services.sdk import resolve_bootstrap_password

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _nextcloud_exec(command: list[str], *, user: str = "www-data", timeout: int = 20) -> tuple[int, str]:
    """Run one OCC command with a bounded timeout during bootstrap."""
    return docker_exec("nextcloud", command, user=user, timeout=timeout)


def bootstrap_nextcloud_admin(config: Config, secrets: dict[str, str]) -> list[str]:
    """Ensure Nextcloud is installed and admin user exists.

    Detects 'installed: false' from occ status and runs initial install if needed.
    Otherwise verifies the admin user already exists.
    """
    logs: list[str] = []
    admin_user = secrets.get("NEXTCLOUD_ADMIN_USER") or "admin"
    admin_pass = resolve_bootstrap_password(secrets, "NEXTCLOUD_ADMIN_PASSWORD")
    db_pass = secrets.get("NEXTCLOUD_DB_PASSWORD", "")
    if not admin_pass:
        logs.append("Nextcloud: SSO_USER_PASSWORD not set — skip admin bootstrap")
        return logs
    if not db_pass:
        logs.append("Nextcloud: NEXTCLOUD_DB_PASSWORD not set — skip admin bootstrap")
        return logs

    for attempt in range(30):
        rc, out = _nextcloud_exec(["php", "occ", "status"])
        if rc == 0 and "installed:" in out:
            break
        time.sleep(10)
    else:
        logs.append(f"Nextcloud: occ not ready after 5min ({out[:120]})")
        return logs

    if "installed: false" in out:
        logs.append("Nextcloud: not installed — running initial setup")
        rc_host, host_out = _nextcloud_exec(["printenv", "POSTGRES_HOST"])
        db_host = (host_out or "").strip().splitlines()[-1] if rc_host == 0 and (host_out or "").strip() else "postgres"
        install_args = [
            "php",
            "occ",
            "maintenance:install",
            "--database",
            "pgsql",
            "--database-name",
            "nextcloud",
            "--database-host",
            db_host,
            "--database-user",
            "nextcloud",
            "--database-pass",
            db_pass,
            "--admin-user",
            admin_user,
            "--admin-pass",
            admin_pass,
        ]
        rc, out = _nextcloud_exec(install_args)
        if rc == 0:
            logs.append("Nextcloud: installation complete")
        else:
            logs.append(f"Nextcloud: install failed ({out[:200]})")
            return logs
    elif "installed: true" in out:
        logs.append("Nextcloud: already installed")

    rc, out = _nextcloud_exec(
        ["php", "occ", "user:list", "--output", "json"],
    )
    if rc == 0 and out.strip():
        try:
            users = json.loads(out.strip())
            if admin_user in users:
                logs.append(f"Nextcloud: admin user {admin_user} exists")
            else:
                logs.append(f"Nextcloud: admin user {admin_user} missing — create manually")
        except json.JSONDecodeError:
            if admin_user in out:
                logs.append(f"Nextcloud: admin user {admin_user} exists")
            else:
                logs.append(f"Nextcloud: admin user {admin_user} not found in occ output")
    else:
        logs.append(f"Nextcloud: could not check admin user ({out[:80]})")

    return logs


def configure_nextcloud_background_jobs() -> list[str]:
    """Ensure Nextcloud uses system cron for background jobs (required for verify)."""
    logs: list[str] = []
    rc, mode_out = _nextcloud_exec(
        ["php", "occ", "config:system:get", "backgroundjobs_mode"],
    )
    current = (mode_out or "").strip().splitlines()[-1] if rc == 0 and (mode_out or "").strip() else ""
    if current != "cron":
        rc_set, out_set = _nextcloud_exec(
            ["php", "occ", "config:system:set", "backgroundjobs_mode", "--value=cron"],
        )
        if rc_set == 0:
            logs.append("Nextcloud: backgroundjobs_mode set to cron")
        else:
            logs.append(f"Nextcloud: backgroundjobs_mode set failed ({(out_set or '')[:80]})")
    else:
        logs.append("Nextcloud: backgroundjobs_mode already cron")
    return logs


def configure_nextcloud_trusted_domain(domain: str) -> list[str]:
    """Ensure Nextcloud trusts the public hostname."""
    logs: list[str] = []
    for _ in range(30):
        rc, output = _nextcloud_exec(["php", "occ", "status"])
        if rc == 0 and "installed: true" in output.lower():
            break
        time.sleep(10)
    else:
        return [f"Nextcloud: occ not ready ({output[:120]})"]

    rc, output = _nextcloud_exec(
        ["php", "occ", "config:system:set", "trusted_domains", "1", "--value", domain],
    )
    if rc == 0:
        logs.append(f"Nextcloud: trusted domain set to {domain}")
    else:
        logs.append(f"Nextcloud: trusted domain update skipped ({output[:120]})")
    return logs


def configure_nextcloud_oidc(config: Config, secrets: dict[str, str]) -> list[str]:
    """Enable Nextcloud OIDC login through the configured identity provider."""
    client_secret = secrets.get("NEXTCLOUD_OIDC_CLIENT_SECRET", "")
    if not client_secret:
        return ["Nextcloud OIDC: NEXTCLOUD_OIDC_CLIENT_SECRET missing - skip"]

    for _ in range(30):
        rc, output = _nextcloud_exec(["php", "occ", "status"])
        if rc == 0 and "installed: true" in output.lower():
            break
        time.sleep(10)
    else:
        return [f"Nextcloud OIDC: occ not ready ({output[:120]})"]

    from toolkit.services.sdk import authelia_oidc_issuer

    issuer = authelia_oidc_issuer(config)
    steps = (
        (["php", "occ", "app:enable", "oidc_login"], "enable oidc_login app"),
        (
            ["php", "occ", "config:app:set", "oidc_login", "oidc_provider_url", "--value", issuer],
            "provider URL",
        ),
        (
            [
                "php",
                "occ",
                "config:app:set",
                "oidc_login",
                "oidc_discovery_endpoint",
                "--value",
                f"{issuer}/.well-known/openid-configuration",
            ],
            "discovery endpoint",
        ),
        (
            ["php", "occ", "config:app:set", "oidc_login", "oidc_client_id", "--value", "nextcloud"],
            "client id",
        ),
        (
            ["php", "occ", "config:app:set", "oidc_login", "oidc_client_secret", "--value", client_secret],
            "client secret",
        ),
        (
            ["php", "occ", "config:app:set", "oidc_login", "oidc_auto_redirect", "--value", "0"],
            "auto redirect off",
        ),
        (
            ["php", "occ", "config:app:set", "oidc_login", "auto_provision", "--value", "1"],
            "auto provision on",
        ),
    )
    logs: list[str] = []
    for command, label in steps:
        rc, output = _nextcloud_exec(command)
        if rc == 0:
            logs.append(f"Nextcloud OIDC: {label} OK")
            continue
        logs.append(f"Nextcloud OIDC: {label} skipped ({output[:100]})")
        if label == "enable oidc_login app":
            break
    return logs
