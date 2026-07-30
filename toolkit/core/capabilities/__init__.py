"""Unified server-capability detection (GPU, ZFS, AES-NI, disk type, CPU model).

Replaces the three previously-scattered GPU detectors:
- toolkit.core.infra.autodetect.detect_hw_transcoding
- toolkit.core.capabilities.detect_gpu_for_vm
- toolkit.core.ops.health_report._detect_gpu

The canonical entrypoint is :func:`detect_capabilities`. Persistence + TTL cache
live in :mod:`toolkit.core.capabilities.store`.
"""

from toolkit.core.capabilities.detect import (
    GpuCapabilities,
    ServerCapabilities,
    detect_capabilities,
    detect_gpu_for_vm,
    detect_gpu_local,
    detect_server_capabilities,
)
from toolkit.core.capabilities.store import load_capabilities

__all__ = [
    "GpuCapabilities",
    "ServerCapabilities",
    "detect_capabilities",
    "detect_gpu_for_vm",
    "detect_gpu_local",
    "detect_server_capabilities",
    "load_capabilities",
]
