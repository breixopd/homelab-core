"""Uptime Kuma first-boot admin setup + HTTP monitor auto-registration.

Uptime Kuma's first-boot requires an admin account to be created via the
setup wizard before the API is usable. This module:
1. Polls the container until it's ready.
2. Creates the admin account via the setup API.
3. Auto-registers HTTP monitors for every public service route.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _kuma_base(cfg: Config) -> str:
    """Return the controller-reachable Uptime Kuma endpoint."""
    from toolkit.core.manifest.placement import service_address

    host = service_address(cfg, "uptime-kuma") if cfg.is_multi_node else "127.0.0.1"
    return f"http://{host}:3001"


def _wait_for_uptime_kuma(base: str, timeout: int = 60) -> bool:
    """Poll Uptime Kuma until its HTTP endpoint responds."""
    for _ in range(timeout // 3):
        try:
            resp = httpx.get(f"{base}/", timeout=5, follow_redirects=False)
            if resp.status_code in (200, 302):
                return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(3)
    return False


def _ensure_database_ready(base: str, timeout: int = 60) -> bool:
    """Complete Uptime Kuma v2's database wizard when it is still active."""
    deadline = time.monotonic() + timeout
    submitted = False

    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base}/api/entry-page", timeout=5)
            response.raise_for_status()
            phase = response.json().get("type")
            if phase != "setup-database":
                return True
            if not submitted:
                setup = httpx.post(
                    f"{base}/setup-database",
                    json={"dbConfig": {"type": "sqlite"}},
                    timeout=15,
                )
                setup.raise_for_status()
                submitted = True
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            # The setup server briefly closes while the main server starts.
            pass
        time.sleep(2)

    return False


def bootstrap_uptime_kuma(cfg: Config, secrets: dict[str, str]) -> list[str]:
    """Create the admin account + register monitors for all public services.

    Uses Uptime Kuma's setup endpoint for its database and the Socket.IO API
    for owner and monitor reconciliation. The admin password is the managed
    homelab SSO password.
    """
    logs: list[str] = []

    base = _kuma_base(cfg)
    if not _wait_for_uptime_kuma(base):
        logs.append("Uptime Kuma: not ready after 60s — skipping admin setup")
        return logs
    if not _ensure_database_ready(base):
        logs.append("Uptime Kuma: database setup did not complete after 60s")
        return logs

    admin_pass = secrets.get("SSO_USER_PASSWORD", "").strip()
    if not admin_pass:
        logs.append("Uptime Kuma: SSO_USER_PASSWORD is missing")
        return logs
    domain = cfg.domain

    try:
        from uptime_kuma_api import MonitorType, UptimeKumaApi
    except ImportError:
        logs.append("Uptime Kuma: v2 API client not installed — manual setup needed")
        logs.append(f"  Visit https://status.{domain} and create admin account manually")
        return logs

    last_error: Exception | None = None
    for attempt in range(1, 4):
        api = None
        attempt_logs: list[str] = []
        try:
            api = UptimeKumaApi(base, timeout=20)
            if api.need_setup():
                api.setup("admin", admin_pass)
                attempt_logs.append("Uptime Kuma: admin account created")

            api.login("admin", admin_pass)
            attempt_logs.append("Uptime Kuma: logged in as admin")

            from toolkit.core.ops.dns import desired_records_from_config

            existing = {monitor.get("name"): monitor for monitor in api.get_monitors()}
            reconciled = 0
            failures: list[str] = []
            for record in desired_records_from_config(cfg, cfg.dns.public_ip):
                if record.type != "A" or not record.name:
                    continue
                if record.name == domain or record.name.startswith("mail."):
                    continue

                name = record.name.removesuffix(f".{domain}")
                url = f"https://{record.name}"
                monitor = existing.get(name)
                try:
                    if monitor is None:
                        api.add_monitor(type=MonitorType.HTTP, name=name, url=url)
                    elif monitor.get("url") != url:
                        api.edit_monitor(int(monitor["id"]), type=MonitorType.HTTP, name=name, url=url)
                    reconciled += 1
                except Exception as exc:
                    detail = str(exc).strip() or type(exc).__name__
                    failures.append(f"{name}: {detail[:80]}")

            if failures:
                raise RuntimeError("; ".join(failures))
            attempt_logs.append(f"Uptime Kuma: reconciled {reconciled} HTTP monitor(s)")
            logs.extend(attempt_logs)
            return logs
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(5)
        finally:
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass

    detail = str(last_error).strip() if last_error is not None else "unknown error"
    if not detail and last_error is not None:
        detail = type(last_error).__name__
    logs.append(f"Uptime Kuma: setup error ({detail[:80]})")
    return logs
