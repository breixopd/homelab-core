"""Tdarr library bootstrap, flow seeding, and media-cache coordination."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from toolkit.core.config.config import Config

TDARR_URL = "http://tdarr:8265"
LIBRARY_FOLDERS = (
    ("Movies", "/data/movies"),
    ("TV", "/data/tv"),
)
FLOW_SEED_NAME = "Homelab — HEVC + health (auto HW)"


def _cruddb(base_url: str, payload: dict[str, Any], *, timeout: float = 30) -> Any:
    resp = httpx.post(
        f"{base_url.rstrip('/')}/api/v2/cruddb",
        json={"data": payload},
        timeout=timeout,
    )
    resp.raise_for_status()
    if not resp.content:
        return [] if payload.get("mode") == "getAll" else {}
    return resp.json()


def ensure_tdarr_plugins(
    base_url: str = TDARR_URL,
    *,
    timeout: int = 300,
    ready: bool | None = None,
) -> list[str]:
    """Verify flow assets, with one bounded refresh attempt when none exist."""
    logs: list[str] = []
    if ready is not True and not wait_for_tdarr(base_url, timeout=60):
        logs.append("Tdarr: API not ready — skip plugin update")
        return logs

    def available_templates() -> int:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/v2/search-flow-templates",
            json={"data": {"string": "", "pluginType": "Community"}},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        templates = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], list) else []
        return len(templates)

    try:
        count = available_templates()
        if count > 0:
            return [f"Tdarr: {count} community flow template(s) available"]
    except (httpx.HTTPError, ValueError):
        pass

    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/api/v2/update-plugins",
            # Tdarr 2.77+ validates the request envelope and requires the
            # force flag.  This is also accepted by older stable releases.
            json={"data": {"force": True}},
            timeout=min(timeout, 30),
        )
        if resp.status_code != 200:
            logs.append(f"Tdarr: update-plugins HTTP {resp.status_code}")
            return logs
        logs.append("Tdarr: triggered community/flow plugin update")
    except httpx.HTTPError as exc:
        logs.append(f"Tdarr: update-plugins request failed ({exc})")
        return logs

    # Optional download endpoint (some versions expose GET download-plugins)
    try:
        dl = httpx.get(f"{base_url.rstrip('/')}/api/v2/download-plugins", timeout=30)
        if dl.status_code == 200:
            logs.append("Tdarr: download-plugins completed")
    except httpx.HTTPError:
        pass

    # Confirm the supported flow-template search API sees the refreshed assets.
    try:
        count = available_templates()
        if count > 0:
            logs.append(f"Tdarr: {count} community flow template(s) available")
        else:
            logs.append("Tdarr: failed to load community flow templates after plugin refresh")
    except (httpx.HTTPError, ValueError):
        logs.append("Tdarr: failed to query community flow templates after plugin refresh")
    return logs


def wait_for_tdarr(base_url: str = TDARR_URL, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url.rstrip('/')}/api/v2/status", timeout=5)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def _existing_library_folders(base_url: str) -> set[str]:
    try:
        data = _cruddb(
            base_url,
            {"collection": "LibrarySettingsJSONDB", "mode": "getAll"},
        )
    except httpx.HTTPError:
        return set()
    folders: set[str] = set()
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                folder = entry.get("folder") or entry.get("folderPath")
                if folder:
                    folders.add(str(folder).rstrip("/"))
    return folders


def _existing_flow_names(base_url: str) -> set[str]:
    try:
        data = _cruddb(base_url, {"collection": "FlowsJSONDB", "mode": "getAll"})
    except httpx.HTTPError:
        return set()
    names: set[str] = set()
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("name"):
                names.add(str(entry["name"]))
    return names


def _flow_seed_path(root: Path | None) -> Path | None:
    if root is None:
        return None
    path = root / "config" / "tdarr" / "bootstrap" / "homelab-hevc-flow.json"
    return path if path.is_file() else None


def ensure_tdarr_flow(
    base_url: str = TDARR_URL,
    *,
    root: Path | None = None,
    ready: bool | None = None,
) -> list[str]:
    """Import homelab flow template if missing."""
    logs: list[str] = []
    if ready is not True and not wait_for_tdarr(base_url, timeout=60):
        logs.append("Tdarr: API not ready — skip flow seed")
        return logs

    if FLOW_SEED_NAME in _existing_flow_names(base_url):
        logs.append(f"Tdarr: flow already present ({FLOW_SEED_NAME})")
        return logs

    seed = _flow_seed_path(root)
    if seed is None:
        logs.append("Tdarr: no flow seed file — add plugins manually in UI")
        return logs

    try:
        obj = json.loads(seed.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logs.append(f"Tdarr: invalid flow seed: {exc}")
        return logs

    doc_id = obj.pop("_id", None) or secrets.token_urlsafe(6)[:12]
    obj.setdefault("name", FLOW_SEED_NAME)
    try:
        _cruddb(
            base_url,
            {
                "collection": "FlowsJSONDB",
                "mode": "insert",
                "docID": doc_id,
                "obj": obj,
            },
        )
        logs.append(f"Tdarr: imported flow {obj.get('name', doc_id)}")
    except httpx.HTTPError as exc:
        logs.append(
            f"Tdarr: flow import failed ({exc}) — install 'Tdarr Flow Plugins' bundle in UI, then re-run deploy hooks"
        )
    return logs


def ensure_tdarr_libraries(
    base_url: str = TDARR_URL,
    *,
    cache_path: str = "/temp",
    schedule: str | None = None,
    flow_id: str | None = None,
    ready: bool | None = None,
) -> list[str]:
    """Create movie/TV libraries if missing. Returns log lines."""
    logs: list[str] = []
    if ready is not True and not wait_for_tdarr(base_url):
        logs.append("Tdarr: API not ready — configure libraries manually in UI")
        return logs

    existing = _existing_library_folders(base_url)
    sched = [{"_id": "offpeak", "cron": schedule, "checked": True}] if schedule else []

    for name, folder in LIBRARY_FOLDERS:
        norm = folder.rstrip("/")
        if norm in existing or any(norm in f for f in existing):
            logs.append(f"Tdarr: library {name} already configured ({folder})")
            continue
        doc_id = secrets.token_urlsafe(6)[:12]
        obj: dict[str, Any] = {
            "name": name,
            "folder": folder,
            "cache": cache_path,
            "processLibrary": True,
            "processTranscodes": True,
            "processHealthChecks": True,
            "folderWatching": True,
            "scannerThreadCount": 2,
            "schedule": sched,
        }
        if flow_id:
            obj["flow"] = flow_id
        try:
            _cruddb(
                base_url,
                {
                    "collection": "LibrarySettingsJSONDB",
                    "mode": "insert",
                    "docID": doc_id,
                    "obj": obj,
                },
            )
            logs.append(f"Tdarr: created library {name} → {folder} (cache {cache_path})")
        except httpx.HTTPError as exc:
            logs.append(f"Tdarr: could not create library {name}: {exc}")

    return logs


def configure_tdarr(
    config: Config,
    *,
    install_root: str | None = None,
    root: Path | None = None,
) -> list[str]:
    """Bootstrap Tdarr libraries, flows, and cache integration (always enabled)."""
    from toolkit.core.ops.automation import resolve_docker_service_url

    repo_root = root or (Path(install_root) if install_root else None)
    from toolkit.core.manifest.placement import service_address

    base_url = resolve_docker_service_url("tdarr", 8265, fallback_host=service_address(config, "tdarr"))
    cache_path = "/temp"
    logs: list[str] = []
    from toolkit.core.manifest.settings import service_enabled

    cache_enabled = service_enabled(config, "media-cache")

    flow_id: str | None = None
    ready = wait_for_tdarr(base_url, timeout=60)
    if not ready:
        logs.append("Tdarr: API not ready — bootstrap deferred")
    else:
        logs.extend(ensure_tdarr_plugins(base_url, ready=True))
        logs.extend(ensure_tdarr_flow(base_url, root=repo_root, ready=True))
        # Re-read flow id only after the API was confirmed available. This lets
        # the library seed reference an imported/existing flow without turning a
        # skipped bootstrap into a direct network request.
        try:
            flows = _cruddb(base_url, {"collection": "FlowsJSONDB", "mode": "getAll"})
            if isinstance(flows, list):
                for entry in flows:
                    if isinstance(entry, dict) and entry.get("name") == FLOW_SEED_NAME:
                        flow_id = entry.get("_id") or entry.get("id")
                        break
        except httpx.HTTPError:
            pass

    # Guardrail: Tdarr must only scan the LOCAL tier (/data/movies, /data/tv).
    # Scanning the rclone union mount (/data/library) would force every cold,
    # remote-only file to be downloaded on each scan/health-check — bandwidth
    # blow-up and cache thrash. Refuse to seed such a library.
    unsafe = [f for _n, f in LIBRARY_FOLDERS if f.rstrip("/").startswith("/data/library")]
    if cache_enabled and unsafe:
        logs.append(
            f"Tdarr: REFUSING libraries under the rclone union mount {unsafe} — "
            "point Tdarr at the local tier (/data/movies, /data/tv) instead"
        )
        return logs

    from toolkit.core.manifest.settings import service_setting_str

    if ready:
        logs.extend(
            ensure_tdarr_libraries(
                base_url,
                cache_path=cache_path,
                schedule=service_setting_str(config, "tdarr", "schedule") or None,
                flow_id=str(flow_id) if flow_id else None,
                ready=True,
            )
        )

    if cache_enabled:
        # Synergy: Tdarr transcodes to HEVC on the local tier (the rclone union's
        # local write upstream) BEFORE media-cache tiers/uploads the file. The
        # smaller HEVC artifact is what gets cached locally and pushed to remote,
        # so more titles fit in the cache and remote egress drops. Pin during the
        # job so eviction can't drop a file mid-transcode.
        logs.append(
            "Tdarr + media-cache synergy: HEVC shrink on the local tier (/data/movies, "
            "/data/tv) feeds smaller files into the rclone union → more fits locally and "
            "remotely. Transcode temp stays on local /temp; long jobs pin via media-cache "
            "POST /api/pin and unpin on completion."
        )
    else:
        logs.append("Tdarr: direct /data transcode (media cache disabled); temp on /temp")

    if install_root:
        from toolkit.core.manifest.placement import service_address

        logs.append(f"Tdarr UI (VPN/LAN): http://{service_address(config, 'tdarr')}:8265")
        logs.append(f"Tdarr UI (private HTTPS): https://tdarr.{config.domain}")
    return logs
