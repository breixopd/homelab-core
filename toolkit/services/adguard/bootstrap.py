"""AdGuard Home post-deploy bootstrap: first-run wizard and rewrite API readiness."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
from toolkit.core.ops.automation import resolve_docker_service_url

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _adguard_is_configured(base: str, password: str) -> bool:
    """Return True if AdGuard accepts login with ``password`` (already configured)."""
    try:
        resp = httpx.post(
            f"{base}/control/login",
            json={"name": "admin", "password": password},
            timeout=10,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def bootstrap_adguard(config: Config, secrets: dict[str, str]) -> list[str]:
    """Complete AdGuard Home's first-run wizard via API so the rewrite API works.

    An unconfigured AdGuard redirects every /control/* call to install.html and
    serves no DNS, which silently breaks internal name resolution.
    """
    logs: list[str] = []
    base = resolve_docker_service_url("adguard", 3000)
    password = secrets.get("ADGUARD_ADMIN_PASSWORD", "")
    if not password:
        logs.append("AdGuard: ADGUARD_ADMIN_PASSWORD not set — skip setup")
        return logs

    # AdGuard may briefly serve the install wizard during startup before
    # loading its config. Retry the status check to avoid a false positive.
    for _attempt in range(10):
        try:
            resp = httpx.get(f"{base}/control/status", timeout=10, follow_redirects=False)
        except httpx.HTTPError:
            time.sleep(2)
            continue
        # 200 = configured and reachable; 401 = configured but needs auth.
        # Both mean first-run is already complete.
        if resp.status_code in (200, 401):
            return logs
        if resp.status_code in (302, 307) and "install" in resp.headers.get("location", ""):
            # AdGuard is showing the install wizard. But after a container
            # restart, AdGuard can briefly serve install.html even when its
            # config file is already populated (race while loading). Confirm
            # by testing login: if the configured password works, AdGuard is
            # actually fine and the 302 is a false positive.
            if _adguard_is_configured(base, password):
                logs.append("AdGuard: already configured (login ok during startup race)")
                return logs
            break  # Genuinely unconfigured — proceed to first-run setup below.
        # Other statuses (500, 502, etc.) — likely still starting. Retry.
        time.sleep(2)
    else:
        # All retries exhausted without a clear signal — try setup anyway.
        pass

    # If we got here, AdGuard redirected to install.html — run first-run setup.
    try:
        resp = httpx.get(f"{base}/control/status", timeout=10, follow_redirects=False)
        if resp.status_code in (200, 401):
            return logs  # Already configured (race resolved during retry loop).
        if resp.status_code in (302, 307) and "install" in resp.headers.get("location", ""):
            # Final guard: if login already works, the wizard redirect is stale.
            if _adguard_is_configured(base, password):
                logs.append("AdGuard: already configured (login ok before setup)")
                return logs
            cfg_resp = httpx.post(
                f"{base}/control/install/configure",
                json={
                    "web": {"ip": "0.0.0.0", "port": 3000},
                    "dns": {"ip": "0.0.0.0", "port": 53},
                    "username": "admin",
                    "password": password,
                },
                timeout=30,
            )
            if cfg_resp.status_code == 200:
                logs.append("AdGuard: first-run setup completed via API")
                _wait_adguard_api_ready(base, password, logs)
            else:
                # Configure endpoint rejected the request. This happens when
                # AdGuard is actually already configured but still serving the
                # install redirect. Confirm via login before declaring failure.
                if _adguard_is_configured(base, password):
                    logs.append("AdGuard: already configured (login ok after rejected setup)")
                else:
                    logs.append(f"AdGuard: setup failed (HTTP {cfg_resp.status_code}: {cfg_resp.text[:120]})")
        else:
            logs.append(f"AdGuard: unexpected status HTTP {resp.status_code} — manual check needed")
    except httpx.HTTPError as exc:
        logs.append(f"AdGuard: setup failed ({exc})")
    return logs


def _wait_adguard_api_ready(base: str, password: str, logs: list[str]) -> None:
    """Poll the AdGuard rewrite API until it returns 200/401 (config reloaded).

    After first-run setup, AdGuard's /control/install/configure returns 200
    immediately, but the DNS server + REST API need to reload their config before
    /control/rewrite/list answers (it 404s during the reload window). Without
    this wait, the plugin's rewrite reconciliation can race the reload and
    abort the deploy.
    """
    auth = ("admin", password)
    for attempt in range(15):
        try:
            resp = httpx.get(f"{base}/control/rewrite/list", timeout=5, auth=auth, follow_redirects=False)
        except httpx.HTTPError:
            time.sleep(2)
            continue
        if resp.status_code in (200, 401):
            if attempt > 0:
                logs.append(f"AdGuard: rewrite API ready after {attempt * 2}s reload wait")
            return
        time.sleep(2)
    logs.append("AdGuard: warning — rewrite API not ready after 30s (sync may race; run 'deploy recover' if it fails)")
