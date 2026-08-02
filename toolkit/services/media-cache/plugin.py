"""media-cache service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml. ``check_backends`` is also exposed as a module-level
function so the external-storage gate can be unit-tested in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services.sdk import VerifyCheck


def check_cache_status(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Functional ``/api/status`` probe — cache manager responds with stats."""
    import json

    import httpx
    from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

    has_storage_host = any("media-cache" in h.services for h in cfg.external_hosts)
    if not has_storage_host:
        return VerifyCheck("media-cache", "cache_status", True, "skipped (no external storage host)")
    if not container_exists_on_vm(cfg, vm_ip, "media-cache", root):
        return VerifyCheck("media-cache", "cache_status", False, "container missing")

    if cfg.is_multi_node:
        rc, body = docker_curl(cfg, vm_ip, "media-cache", "http://localhost:8686/api/status", root=root)
        if rc != 0 or not (body or "").strip():
            return VerifyCheck("media-cache", "cache_status", False, "status API unreachable")
        text = body
    else:
        try:
            resp = httpx.get("http://localhost:8686/api/status", timeout=10)
            if resp.status_code != 200:
                return VerifyCheck("media-cache", "cache_status", False, f"HTTP {resp.status_code}")
            text = resp.text
        except httpx.HTTPError:
            return VerifyCheck("media-cache", "cache_status", False, "API unreachable")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return VerifyCheck("media-cache", "cache_status", False, "invalid JSON")
    if not isinstance(data, dict):
        return VerifyCheck("media-cache", "cache_status", False, "unexpected response")
    max_gb = data.get("cache_max_gb", 0)
    detail = f"cache_max_gb={max_gb}, tracked={data.get('tracked_files', 0)}"
    return VerifyCheck("media-cache", "cache_status", True, detail)


def check_health(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    import httpx
    from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

    has_storage_host = any("media-cache" in h.services for h in cfg.external_hosts)
    if not has_storage_host:
        return VerifyCheck("media-cache", "health", True, "skipped (no external storage host)")
    if not container_exists_on_vm(cfg, vm_ip, "media-cache", root):
        return VerifyCheck("media-cache", "health", False, "container missing")
    if cfg.is_multi_node:
        rc, body = docker_curl(cfg, vm_ip, "media-cache", "http://localhost:8686/health", root=root)
        ok = rc == 0 and "ok" in (body or "").lower()
    else:
        try:
            resp = httpx.get("http://localhost:8686/health", timeout=8)
            ok = resp.status_code == 200 and resp.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            ok = False
    return VerifyCheck("media-cache", "health", ok, "healthy" if ok else "unreachable")


def check_backends(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """media-cache backends gate: verify-skip when no external storage host."""
    import json

    import httpx
    from toolkit.services.sdk import VerifyCheck, docker_curl

    has_storage_host = any("media-cache" in h.services for h in cfg.external_hosts)
    if not has_storage_host:
        return VerifyCheck(
            "media-cache",
            "backends",
            True,
            "skipped: no external_hosts entry carries the 'media-cache' service (configure a NAS to enable)",
        )
    if cfg.is_multi_node:
        rc, body = docker_curl(cfg, vm_ip, "media-cache", "http://localhost:8686/api/backends", root=root)
        if rc != 0 or not (body or "").strip():
            return VerifyCheck("media-cache", "backends", False, "backends API unreachable")
        text = body
    else:
        try:
            resp = httpx.get("http://localhost:8686/api/backends", timeout=10)
            if resp.status_code != 200:
                return VerifyCheck("media-cache", "backends", False, f"HTTP {resp.status_code}")
            text = resp.text
        except httpx.HTTPError:
            return VerifyCheck("media-cache", "backends", False, "API unreachable")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return VerifyCheck("media-cache", "backends", False, "backends API returned invalid JSON")
    backends = data.get("backends") if isinstance(data, dict) else None
    if not isinstance(backends, list):
        return VerifyCheck("media-cache", "backends", False, "backends response missing 'backends' list")
    count = len(backends)
    names = ", ".join(str(b) for b in backends if b) or "none"
    return VerifyCheck("media-cache", "backends", count > 0, f"{count} backend(s): {names}")


class MediaCachePlugin(ServicePlugin):
    service = "media-cache"
    category = "media"

    def reconcile_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
        *,
        selected: bool,
    ) -> list[str]:
        from .client import render_external_media_cache_config

        if integration != "media-cache":
            raise ValueError(f"unsupported media-cache host integration: {integration}")
        path, count = render_external_media_cache_config(root)
        state = "joined" if selected else "removed"
        if count:
            return [
                f"Media cache: {host.name} {state}; projected {count} storage host(s) to {path.name}",
                "Media cache: runtime refresh requested for the media node",
            ]
        return [f"Media cache: {host.name} {state}; storage pool is empty"]

    def cleanup_host_integration(self, integration: str, cfg: Config, host: ExternalHost, root: Path) -> list[str]:
        from .client import render_external_media_cache_config

        if integration != "media-cache":
            raise ValueError(f"unsupported media-cache host integration: {integration}")
        path, count = render_external_media_cache_config(root, excluded_names={host.name})
        if count:
            return [
                f"Media cache: removed {host.name}; projected {count} remaining storage host(s) to {path.name}",
                "Media cache: runtime refresh requested for the media node",
            ]
        return [f"Media cache: removed {host.name}; storage pool is empty"]

    def host_integration_refresh_nodes(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        *,
        selected: bool,
    ) -> tuple[str, ...]:
        if integration != "media-cache":
            raise ValueError(f"unsupported media-cache host integration: {integration}")
        if not self.is_enabled(cfg):
            return ()
        return (self.runtime_node(cfg),)

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Confirm cache readiness and reconcile playback webhooks."""
        from toolkit.core.ops.automation import resolve_docker_service_url

        from .client import MediaCacheClient, register_tautulli_webhook

        if not self.is_enabled(cfg):
            return []
        try:
            cache = MediaCacheClient(
                base_url=resolve_docker_service_url("media-cache", 8686),
                token=secrets.get("MEDIA_CACHE_TOKEN", ""),
            )
            if not cache.health():
                return ["WARNING: media-cache: not ready yet"]
        except (OSError, ValueError, RuntimeError) as exc:
            return [f"WARNING: media-cache health check failed ({exc})"]

        logs = ["Media cache running"]
        from toolkit.core.manifest.settings import service_setting_str

        server = service_setting_str(cfg, "media-library", "server")
        if server in ("jellyfin", "both"):
            logs.append("media-cache: Jellyfin webhook owned by the Jellyfin plugin")
        if server not in ("plex", "both"):
            return logs

        tautulli_key = secrets.get("TAUTULLI_API_KEY", "")
        if not tautulli_key:
            logs.append("WARNING: media-cache: TAUTULLI_API_KEY missing - configure Plex webhook via Tautulli UI")
            return logs
        try:
            ok, message = register_tautulli_webhook(
                tautulli_url=resolve_docker_service_url("tautulli", 8181),
                api_key=tautulli_key,
                webhook_url="http://media-cache:8686/webhook/tautulli",
                webhook_token=secrets.get("MEDIA_CACHE_WEBHOOK_TOKEN", ""),
            )
            if ok:
                logs.append(f"media-cache: registered tautulli webhook ({message})")
            else:
                logs.append(f"WARNING: media-cache: tautulli webhook registration failed ({message})")
        except Exception as exc:
            logs.append(f"WARNING: media-cache: tautulli webhook registration failed ({exc})")
        return logs

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        import json

        from toolkit.services.sdk import docker_curl

        vm_ip = self.runtime_address(cfg)
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "media-cache",
            "http://localhost:8686/api/backends",
            root=root,
            timeout=5,
        )
        if rc != 0 or not body or len(body.encode("utf-8")) > 64 * 1024:
            raise RuntimeError("media-cache backend API is unavailable")
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("media-cache backend API returned invalid data") from exc
        backends = data.get("backends") if isinstance(data, dict) else None
        if not isinstance(backends, list):
            raise RuntimeError("media-cache backend API returned invalid data")
        rows: list[dict[str, object]] = []
        for name in backends[:100]:
            if not isinstance(name, str):
                continue
            if name == "media-union":
                kind = "Union pool"
            elif name.startswith("ext-"):
                kind = "Fleet storage"
            else:
                kind = "Storage"
            rows.append({"name": name, "kind": kind})
        return {"storage_backends": rows}

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        import json

        from toolkit.services.sdk import docker_curl

        vm_ip = self.runtime_address(cfg)
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "media-cache",
            "http://localhost:8686/api/status",
            root=root,
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
        bandwidth = data.get("bandwidth")
        uplink = bandwidth.get("effective_uplink_mbps") if isinstance(bandwidth, dict) else None
        candidates = {
            "cache_used_pct": data.get("cache_used_pct"),
            "tracked_files": data.get("tracked_files"),
            "active_prefetch": data.get("active_prefetch"),
            "effective_uplink_mbps": uplink,
        }
        return {
            key: value
            for key, value in candidates.items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """media-cache health, backends, and functional status API (storage-gated)."""
        return [check_health(cfg, vm_ip, root), check_backends(cfg, vm_ip, root), check_cache_status(cfg, vm_ip, root)]
