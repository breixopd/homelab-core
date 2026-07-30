"""qBittorrent post-deploy bootstrap: WebUI credential rotation and config seeding."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from toolkit.core.ops.automation import docker_curl, docker_exec

logger = logging.getLogger(__name__)


def _qbit_temp_password_from_logs(container: str) -> str | None:
    """qBittorrent 5.x issues a one-time WebUI password in container logs when unset."""
    import re
    import subprocess

    started_at = ""
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        started_at = (inspect.stdout or "").strip()
    except OSError:
        pass

    log_cmd = ["docker", "logs"]
    if started_at:
        log_cmd.extend(["--since", started_at])
    log_cmd.append(container)
    try:
        proc = subprocess.run(
            log_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except OSError:
        return None
    matches = re.findall(r"temporary password is provided for this session:\s*(\S+)", text)
    return matches[-1] if matches else None


def _qbit_pbkdf2_hash(password: str) -> str:
    """Generate a qBittorrent-compatible PBKDF2-HMAC-SHA512 password hash.

    Format: ``base64(salt):base64(hash)`` with 16-byte salt and 100k iterations,
    matching what qBittorrent writes to ``WebUI\\Password_PBKDF2`` in qBittorrent.conf.
    """
    import base64
    import hashlib
    import secrets as _secrets

    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000, dklen=64)
    return f"{base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()}"


def _seed_qbittorrent_config_password(container: str, user: str, password: str) -> bool:
    """Write the qBittorrent WebUI credentials directly to qBittorrent.conf.

    This is the deterministic fallback when API-based credential rotation fails
    (e.g. temp password unavailable, login bypass issues). Writes the PBKDF2 hash
    and username into the config file, then restarts the container so qBittorrent
    picks up the new credentials on next start.
    """
    import subprocess

    pbkdf2 = _qbit_pbkdf2_hash(password)
    # qBittorrent.conf uses INI escaping: backslashes and @ByteArray() wrapper.
    # Also force WebUI\Address=0.0.0.0 — qBittorrent 5.x defaults to IPv6 localhost
    # (::1) which makes the WebUI unreachable from other containers (Sonarr/Radarr).
    script = (
        "CONF=/config/qBittorrent/qBittorrent.conf; "
        '[ -f "$CONF" ] || exit 1; '
        # Remove any existing username/password/address lines.
        "sed -i '/WebUI\\\\Password_PBKDF2/d;/WebUI\\\\Username/d;/WebUI\\\\Address/d' \"$CONF\"; "
        # Ensure [Preferences] section exists.
        "grep -q '^\\[Preferences\\]' \"$CONF\" || echo '[Preferences]' >> \"$CONF\"; "
        # Append the new credentials and bind WebUI to all IPv4 interfaces.
        "printf 'WebUI\\\\Address=0.0.0.0\\n' >> \"$CONF\"; "
        'printf \'WebUI\\\\Username=%s\\n\' "$QBITTORRENT_WEBUI_USERNAME" >> "$CONF"; '
        'printf \'WebUI\\\\Password_PBKDF2="@ByteArray(%s)"\\n\' "$QBITTORRENT_WEBUI_PASSWORD_PBKDF2" >> "$CONF"; '
        "echo ok"
    )
    rc, out = docker_exec(
        container,
        ["sh", "-ec", script],
        secret_environment={
            "QBITTORRENT_WEBUI_USERNAME": user,
            "QBITTORRENT_WEBUI_PASSWORD_PBKDF2": pbkdf2,
        },
    )
    if rc != 0 or "ok" not in (out or ""):
        return False
    subprocess.run(["docker", "restart", container], check=False, timeout=90, capture_output=True)
    time.sleep(20)
    return True


def _ensure_qbittorrent_webui_address(container: str) -> bool:
    """Ensure qBittorrent WebUI binds to 0.0.0.0 (not IPv6 localhost).

    qBittorrent 5.x on linuxserver binds to ::1 when WebUI\\Address is '*' or
    unset, making the WebUI unreachable from sibling containers (Sonarr/Radarr)
    that connect via the Docker bridge (IPv4). This is a no-op if already set.
    """
    import subprocess

    rc, out = docker_exec(
        container,
        ["sh", "-c", "grep -q '^WebUI\\\\Address=0\\.0\\.0\\.0' /config/qBittorrent/qBittorrent.conf 2>/dev/null"],
    )
    if rc == 0:
        return True  # Already correctly set.
    rc, _ = docker_exec(
        container,
        [
            "sh",
            "-c",
            "sed -i 's/^WebUI\\\\Address=.*/WebUI\\\\Address=0.0.0.0/' "
            "/config/qBittorrent/qBittorrent.conf 2>/dev/null; "
            "grep -q '^WebUI\\\\Address=' /config/qBittorrent/qBittorrent.conf 2>/dev/null || "
            "printf 'WebUI\\\\Address=0.0.0.0\\n' >> /config/qBittorrent/qBittorrent.conf; "
            "echo done",
        ],
    )
    if rc != 0:
        return False
    subprocess.run(["docker", "restart", container], check=False, timeout=90, capture_output=True)
    time.sleep(15)
    return True


def _reset_qbittorrent_webui_lock(container: str) -> bool:
    """Clear WebUI credentials so linuxserver qBittorrent accepts default admin/adminadmin."""
    import subprocess

    rc, _ = docker_exec(
        container,
        [
            "sh",
            "-c",
            "sed -i '/WebUI\\\\Password_PBKDF2/d;/WebUI\\\\Username/d' "
            "/config/qBittorrent/qBittorrent.conf 2>/dev/null || true",
        ],
    )
    if rc != 0:
        return False
    subprocess.run(["docker", "restart", container], check=False, timeout=90, capture_output=True)
    time.sleep(20)
    return True


def _ensure_qbittorrent_save_path(container: str, user: str, password: str, save_path: str = "/data/downloads") -> bool:
    """Point qBittorrent at the mounted media path when the default /downloads is absent."""
    import json as _json

    login_rc, _ = docker_curl(
        container,
        "http://127.0.0.1:8080/api/v2/auth/login",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"username": user, "password": password}),
        cookie_jar="/tmp/qbt-cookies",
    )
    if login_rc != 0:
        return False
    rc, out = docker_curl(
        container,
        "http://127.0.0.1:8080/api/v2/app/preferences",
        cookie_file="/tmp/qbt-cookies",
    )
    if rc != 0 or not out:
        return False
    try:
        prefs = _json.loads((out or b"").decode() if isinstance(out, bytes) else out)
    except _json.JSONDecodeError:
        return False
    current = str(prefs.get("save_path", "") or prefs.get("savePath", ""))
    if current == save_path:
        return True
    for predicate in ("-d", "-w"):
        check_rc, _ = docker_exec(container, ["test", predicate, save_path])
        if check_rc != 0:
            return False
    payload = _json.dumps({"save_path": save_path, "temp_path": f"{save_path}/incomplete"})
    rc, _ = docker_curl(
        container,
        "http://127.0.0.1:8080/api/v2/app/setPreferences",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"json": payload}),
        cookie_file="/tmp/qbt-cookies",
    )
    return rc == 0


def bootstrap_qbittorrent_credentials(secrets: dict[str, str], *, service_host: str = "qbittorrent") -> list[str]:
    logs: list[str] = []
    user = (secrets.get("QBITTORRENT_USER") or "admin").strip() or "admin"
    password = secrets.get("QBITTORRENT_PASSWORD", "")
    if not password:
        logs.append("qBittorrent: QBITTORRENT_PASSWORD not set — skip credential bootstrap")
        return logs

    hosts = [service_host]
    if service_host == "gluetun":
        hosts.extend(["qbittorrent-vpn", "qbittorrent"])

    def _try_login(container: str, login_user: str, login_pass: str) -> bool:
        rc, out = docker_curl(
            container,
            "http://127.0.0.1:8080/api/v2/auth/login",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({"username": login_user, "password": login_pass}),
            cookie_jar="/tmp/qbt-cookies",
        )
        if rc != 0:
            return False
        text = (out or "").strip()
        if "Unauthorized" in text or "Fails." in text:
            return False
        return "Ok" in text or not text

    for host in dict.fromkeys(hosts):
        container = "qbittorrent-vpn" if host in ("gluetun", "qbittorrent-vpn") else host
        # Ensure WebUI is reachable from sibling containers (bind 0.0.0.0, not ::1).
        if _ensure_qbittorrent_webui_address(container):
            logs.append(f"qBittorrent: WebUI bound to 0.0.0.0 on {container}")
        if _try_login(container, user, password):
            logs.append(f"qBittorrent: credentials verified via {container}")
            if _ensure_qbittorrent_save_path(container, user, password):
                logs.append(f"qBittorrent: save path set to /data/downloads on {container}")
            return logs

        login_candidates: list[tuple[str, str]] = [("admin", "adminadmin")]
        temp_pass = _qbit_temp_password_from_logs(container)
        if temp_pass:
            login_candidates.insert(0, ("admin", temp_pass))
        if not temp_pass:
            if _reset_qbittorrent_webui_lock(container):
                logs.append(f"qBittorrent: reset WebUI lock on {container} — waiting for temp password")
                temp_pass = _qbit_temp_password_from_logs(container)
                if temp_pass:
                    login_candidates.insert(0, ("admin", temp_pass))

        logged_in = False
        for login_user, login_pass in login_candidates:
            if _try_login(container, login_user, login_pass):
                logged_in = True
                break
        if not logged_in:
            # Last resort: write the PBKDF2 hash directly to qBittorrent.conf
            # and restart so the container picks up the secret password.
            logs.append(f"qBittorrent: no login worked via {container} — seeding config file directly")
            if _seed_qbittorrent_config_password(container, user, password):
                if _try_login(container, user, password):
                    logs.append(f"qBittorrent: WebUI credentials seeded via config file on {container}")
                    return logs
                logs.append(f"qBittorrent: config seed completed but login verify failed via {container}")
            continue

        logger.warning("qBittorrent: rotating WebUI credentials to secrets value")
        import json as _json

        prefs = _json.dumps({"web_ui_username": user, "web_ui_password": password})
        rc, out = docker_curl(
            container,
            "http://127.0.0.1:8080/api/v2/app/setPreferences",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({"json": prefs}),
            cookie_file="/tmp/qbt-cookies",
        )
        if rc == 0 and _try_login(container, user, password):
            logs.append(f"qBittorrent: WebUI credentials configured via {container}")
            if _ensure_qbittorrent_save_path(container, user, password):
                logs.append(f"qBittorrent: save path set to /data/downloads on {container}")
            return logs

        # Fallback: write the PBKDF2 hash directly to qBittorrent.conf and restart.
        # This is deterministic and doesn't depend on API login working.
        logs.append(f"qBittorrent: API rotation failed via {container} — seeding config file directly")
        if _seed_qbittorrent_config_password(container, user, password):
            if _try_login(container, user, password):
                logs.append(f"qBittorrent: WebUI credentials seeded via config file on {container}")
                if _ensure_qbittorrent_save_path(container, user, password):
                    logs.append(f"qBittorrent: save path set to /data/downloads on {container}")
                return logs
            logs.append(f"qBittorrent: config seed completed but login verify failed via {container}")
        else:
            logs.append(f"qBittorrent: config seed failed via {container} ({(out or '')[:80]})")
        return logs

    # Arr clients may already be wired even when WebUI login probe fails.
    logs.append("qBittorrent: WebUI login probe failed — Sonarr/Radarr download clients may still work")
    return logs
