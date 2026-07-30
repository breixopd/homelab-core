"""Adaptive Tdarr worker sizing from generic host capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.capabilities import GpuCapabilities, detect_server_capabilities
from toolkit.core.infra.host_capacity import HostCapacity

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def recommend_cpu_workers(capacity: HostCapacity) -> int:
    """Leave CPU and memory headroom for playback and library services."""
    reserve = 2 if capacity.cpu_cores > 4 else 1
    by_cpu = max(1, capacity.cpu_cores - reserve)
    by_memory = max(1, capacity.mem_total_mb // 2048)
    return max(1, min(4, by_cpu, by_memory))


def recommend_gpu_workers(capacity: HostCapacity, gpu: GpuCapabilities) -> int:
    if not gpu.has_gpu_transcode or capacity.cpu_cores <= 2:
        return 0
    return 1


def resolve_cpu_workers(cfg: Config, *, root: Path | None = None, fast: bool = True) -> int:
    from toolkit.core.manifest.placement import service_node
    from toolkit.core.manifest.settings import service_setting_int

    configured = service_setting_int(cfg, "tdarr", "cpu-workers")
    if configured > 0:
        return max(1, min(8, configured))
    capabilities = detect_server_capabilities(cfg, vm=service_node(cfg, "tdarr"), root=root, fast=fast)
    return recommend_cpu_workers(capabilities.host)


def resolve_gpu_workers(cfg: Config, *, root: Path | None = None, fast: bool = True) -> int:
    from toolkit.core.manifest.placement import service_node
    from toolkit.core.manifest.settings import service_setting_int

    configured = service_setting_int(cfg, "tdarr", "gpu-workers")
    if configured >= 0:
        return max(0, min(2, configured))
    capabilities = detect_server_capabilities(cfg, vm=service_node(cfg, "tdarr"), root=root, fast=fast)
    return recommend_gpu_workers(capabilities.host, capabilities.gpu)
