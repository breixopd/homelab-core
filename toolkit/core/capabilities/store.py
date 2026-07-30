"""Capability persistence + TTL cache.

State files live under ``<root>/generated/server-capabilities-<vm>.json`` (one per
VM role). The default TTL is 1 hour; override via the
``HOMELAB_CAPABILITIES_TTL_SECONDS`` environment variable.

``load_capabilities(vm, root=..., force_refresh=False)`` returns the cached
snapshot when fresh, redetects otherwise, and always best-effort persists the
fresh result. Detection failures fall back to the cached snapshot if one exists
(even if stale) so a transient probe outage never blocks callers.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from toolkit.core.capabilities.detect import GpuCapabilities, ServerCapabilities, detect_capabilities

_DEFAULT_TTL_S = 3600


def _generated_dir(root: Path | str = ".") -> Path:
    return Path(root) / "generated"


def _cache_path(vm: str, root: Path | str = ".") -> Path:
    safe_vm = vm.replace("/", "_") or "local"
    return _generated_dir(root) / f"server-capabilities-{safe_vm}.json"


def _ttl_seconds() -> int:
    raw = os.environ.get("HOMELAB_CAPABILITIES_TTL_SECONDS")
    try:
        return int(raw) if raw else _DEFAULT_TTL_S
    except ValueError:
        return _DEFAULT_TTL_S


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_s = time.time() - path.stat().st_mtime
    return age_s < _ttl_seconds()


def _from_dict(data: dict) -> ServerCapabilities:
    """Rehydrate a ServerCapabilities from the persisted JSON dict."""
    from toolkit.core.infra.host_capacity import HostCapacity

    host_data = data.get("host", {})
    host = HostCapacity(
        cpu_cores=host_data.get("cpu_cores", 0),
        mem_total_mb=host_data.get("mem_total_mb", 0),
        load_1m=host_data.get("load_1m", 0.0),
        wave_timeout_s=host_data.get("wave_timeout_s", 180),
        inter_wave_sleep_s=host_data.get("inter_wave_sleep_s", 5),
        max_pull_parallel=host_data.get("max_pull_parallel", 2),
        load_threshold=host_data.get("load_threshold", 8.0),
        source=host_data.get("source", "cache"),
    )
    gpu_data = data.get("gpu", {})
    gpu = GpuCapabilities(
        backend=gpu_data.get("backend", "none"),
        name=gpu_data.get("name", ""),
        vram_mb=gpu_data.get("vram_mb"),
        device_nodes=tuple(gpu_data.get("device_nodes", []) or []),
        source=gpu_data.get("source", "cache"),
    )
    return ServerCapabilities(
        host=host,
        gpu=gpu,
        vm=data.get("vm", "local"),
        has_aes_ni=data.get("has_aes_ni", False),
        disk_type=data.get("disk_type", "unknown"),
        cpu_model=data.get("cpu_model", ""),
        detected_at=data.get("detected_at", ""),
    )


def load_capabilities(
    vm: str = "local",
    *,
    root: Path | str = ".",
    force_refresh: bool = False,
) -> ServerCapabilities:
    """Return cached capabilities or detect fresh.

    - Honors a TTL (default 1 h, env-overridable) on the persisted JSON cache.
    - ``force_refresh=True`` bypasses the cache and redetects.
    - If detection raises, falls back to the stale cache (if any) before
      re-raising — transient probe outages shouldn't break callers.
    """
    cache = _cache_path(vm, root)
    if not force_refresh and _is_fresh(cache):
        try:
            data = json.loads(cache.read_text())
            return _from_dict(data)
        except (OSError, ValueError, KeyError):
            pass  # corrupt/malformed cache — fall through to detect

    try:
        caps = detect_capabilities(vm=vm, force_refresh=force_refresh)
    except Exception:
        # Last resort: serve the stale cache if it exists; otherwise re-raise.
        if cache.exists():
            try:
                return _from_dict(json.loads(cache.read_text()))
            except (OSError, ValueError, KeyError):
                pass
        raise

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(caps.to_dict(), indent=2))
    except OSError:
        pass  # best-effort persistence
    return caps
