"""music-sync service plugin.

Owns post_start() (initial sync trigger) and verify() (health + API status) on
top of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class MusicSyncPlugin(ServicePlugin):
    service = "music-sync"
    category = "media"

    def supported_actions(self) -> frozenset[str]:
        return frozenset({"sync-now"})

    def execute_action(
        self,
        action: str,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> list[str]:
        if action != "sync-now":
            raise ValueError("unsupported music-sync action")
        from toolkit.services.sdk import basic_auth_header, docker_curl

        username = secrets.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
        password = secrets.get("MUSIC_SYNC_WEB_PASSWORD", "")
        headers = {"Authorization": basic_auth_header(username, password)} if password else None
        vm_ip = self.runtime_address(cfg)
        rc, _output = docker_curl(
            cfg,
            vm_ip,
            "music-sync",
            "http://localhost:8845/api/sync",
            root=root,
            headers=headers,
            method="POST",
            timeout=20,
        )
        if rc != 0:
            raise RuntimeError("music synchronization request failed")
        return ["Music synchronization accepted"]

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        from toolkit.services.sdk import basic_auth_header, docker_curl

        username = secrets.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
        password = secrets.get("MUSIC_SYNC_WEB_PASSWORD", "")
        headers = {"Authorization": basic_auth_header(username, password)} if password else None
        vm_ip = self.runtime_address(cfg)
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "music-sync",
            "http://localhost:8845/api/status",
            root=root,
            headers=headers,
            timeout=5,
        )
        if rc != 0 or not body or len(body.encode("utf-8")) > 64 * 1024:
            return {}
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key in ("tracks", "playlists", "heartbeat_age_seconds")
            if isinstance((value := data.get(key)), int | float) and not isinstance(value, bool)
        }

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Trigger the first music-sync run so the library populates immediately."""
        if not self.is_enabled(cfg):
            return []

        from toolkit.services.sdk import basic_auth_header, docker_curl

        install_root = root or Path.cwd()
        vm_ip = self.runtime_address(cfg)
        rc, _ = docker_curl(cfg, vm_ip, "music-sync", "http://localhost:8845/health", root=install_root)
        if rc != 0:
            return ["WARNING: music-sync: not ready yet"]

        username = secrets.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
        password = secrets.get("MUSIC_SYNC_WEB_PASSWORD", "")
        headers = {"Authorization": basic_auth_header(username, password)} if password else None

        rc, out = docker_curl(
            cfg,
            vm_ip,
            "music-sync",
            "http://localhost:8845/api/sync",
            root=install_root,
            headers=headers,
            method="POST",
            timeout=20,
        )
        if rc == 0:
            return ["  music-sync: triggered initial sync"]
        detail = _sanitize_status_message(out) if out else ""
        return [f"WARNING: music-sync: initial sync trigger failed{f' ({detail})' if detail else ''}"]

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, basic_auth_header, container_exists_on_vm, docker_curl

        if not self.is_enabled(cfg):
            return [VerifyCheck("music-sync", "health", True, "skipped (music sync disabled)")]

        if not container_exists_on_vm(cfg, vm_ip, "music-sync", root):
            return [VerifyCheck("music-sync", "health", False, "container missing")]

        rc, _ = docker_curl(cfg, vm_ip, "music-sync", "http://localhost:8845/health", root=root)
        health_ok = rc == 0
        checks: list[VerifyCheck] = [
            VerifyCheck("music-sync", "health", health_ok, "reachable" if health_ok else "unreachable"),
        ]
        if not health_ok:
            return checks

        username = secrets.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
        password = secrets.get("MUSIC_SYNC_WEB_PASSWORD", "")
        headers = {"Authorization": basic_auth_header(username, password)} if password else None
        rc_status, body = docker_curl(
            cfg, vm_ip, "music-sync", "http://localhost:8845/api/status", root=root, headers=headers
        )
        if rc_status != 0 or not body:
            checks.append(VerifyCheck("music-sync", "api_status", False, "status API unreachable"))
            return checks
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeError):
            checks.append(VerifyCheck("music-sync", "api_status", False, "invalid status JSON"))
            return checks
        if not isinstance(data, dict):
            checks.append(VerifyCheck("music-sync", "api_status", False, "invalid status shape"))
            return checks

        running = data.get("running")
        if not isinstance(running, bool):
            checks.append(VerifyCheck("music-sync", "api_status", False, "status running must be boolean"))
            return checks
        tracks = data.get("tracks", 0)
        raw_sync_status = data.get("sync_status")
        if not isinstance(raw_sync_status, str) or raw_sync_status.strip().lower() not in {
            "running",
            "success",
            "failed",
        }:
            checks.append(VerifyCheck("music-sync", "api_status", False, "status has invalid sync_status"))
            return checks
        sync_status = raw_sync_status.strip().lower()
        sources = data.get("sync_sources")
        if not isinstance(sources, list):
            checks.append(VerifyCheck("music-sync", "api_status", False, "status missing sync_sources"))
            return checks

        # The status API is authoritative about source configuration.  Keep
        # optional, unconfigured sources from failing verification while
        # requiring every configured source to be OAuth-ready.
        configured_not_ready: list[str] = []
        configured_failed: list[str] = []
        configured_sources: set[str] = set()
        for item in sources:
            if not isinstance(item, dict):
                checks.append(VerifyCheck("music-sync", "api_status", False, "status has invalid sync source"))
                return checks
            raw_name = item.get("name")
            configured = item.get("configured")
            success = item.get("success")
            if not isinstance(raw_name, str) or not raw_name.strip() or not isinstance(configured, bool):
                checks.append(VerifyCheck("music-sync", "api_status", False, "status has invalid sync source"))
                return checks
            if not isinstance(success, bool):
                checks.append(VerifyCheck("music-sync", "api_status", False, "status has invalid sync result"))
                return checks
            name = raw_name.strip().lower()
            if configured:
                configured_sources.add(name)
                if not success:
                    configured_failed.append(name)

        # Top-level readiness flags are used by the live API for the two
        # supported providers.  Only evaluate a flag when that source is
        # represented as configured by sync_sources.
        for name, field in (("spotify", "spotify_ready"), ("ytmusic", "ytmusic_ready")):
            ready = data.get(field)
            if not isinstance(ready, bool):
                checks.append(VerifyCheck("music-sync", "api_status", False, f"status {field} must be boolean"))
                return checks
            if name in configured_sources and not ready:
                configured_not_ready.append(name)

        status_failed = sync_status == "failed"
        authorization_pending = set(configured_not_ready)
        operational_failures = sorted(set(configured_failed) - authorization_pending)
        detail = (
            f"status={sync_status or 'unknown'}, running={running}, "
            f"playlists={data.get('playlists', 0)}, tracks={tracks}"
        )
        if configured_not_ready:
            names = ", ".join(sorted(set(configured_not_ready)))
            detail += f"; manual authorization required for configured source(s): {names}"
        if operational_failures:
            names = ", ".join(operational_failures)
            detail += f"; configured source synchronization failed: {names}"
        if status_failed and not authorization_pending:
            detail += "; latest synchronization failed"
        warnings = data.get("warnings")
        if isinstance(warnings, list) and warnings:
            safe = [message for warning in warnings if (message := _sanitize_status_message(warning))]
            if safe:
                detail += "; warnings: " + ", ".join(safe[:3])
        # An OAuth source awaiting its first operator authorization is a manual
        # setup state, not a broken deployment. Its per-source and top-level
        # sync failures are derivative until authorization is complete. A
        # ready source that fails synchronization remains blocking.
        passed = running and not operational_failures and (not status_failed or bool(authorization_pending))
        retryable = not authorization_pending
        checks.append(VerifyCheck("music-sync", "api_status", passed, detail, retryable=retryable))
        return checks


_TOKEN_RE = re.compile(r"(?i)(?:token|secret|password|apikey|api[-_ ]?key)\s*[=:]\s*[^,;\s]+")
_BEARER_RE = re.compile(r"(?i)(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]+")


def _sanitize_status_message(value: object) -> str:
    """Return bounded, non-sensitive warning text suitable for verify output."""
    if not isinstance(value, str):
        return "[non-text warning omitted]"
    text = " ".join(value.split())
    text = _TOKEN_RE.sub(lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + "=[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    return text[:160]
