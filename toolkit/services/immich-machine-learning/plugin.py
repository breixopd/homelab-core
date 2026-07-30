"""immich-machine-learning service plugin — GPU-adaptive.

compose_service() queries the unified capability detector to pick the right
ML runtime + device mounts:
- nvidia GPU  → runtime: nvidia + NVIDIA_VISIBLE_DEVICES=all (CUDA)
- vaapi (Intel/AMD iGPU) → mounts /dev/dri (OpenCL/VAAPI)
- none        → CPU-only (the shipped default)

Degrades gracefully: if capability detection is unavailable (e.g., running on
the controller during generate), the CPU path is returned. The GPU path only
engages when load_capabilities reports has_gpu and a matching backend.

This is the canonical consumer of the capability cache: the same
server-capabilities.json that drives media transcode profile selection now
also drives ML runtime selection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class ImmichMachineLearningPlugin(ServicePlugin):
    service = "immich-machine-learning"
    category = "cloud"

    def compose_service(self, cfg: Config | None = None) -> dict:
        """Return the GPU-augmented compose block when a GPU is detected.

        Reads the base compose.yaml, then overlays GPU runtime/device mounts
        based on load_capabilities(). CPU-only is the safe fallback.
        """
        service = super().compose_service(cfg)

        caps = _safe_load_capabilities(cfg)
        if caps is None or not caps.has_gpu:
            return service

        if caps.gpu.backend == "nvidia":
            service["runtime"] = "nvidia"
            env = service.setdefault("environment", {})
            env["NVIDIA_VISIBLE_DEVICES"] = "all"
            env["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
        elif caps.gpu.backend == "vaapi":
            devices = service.setdefault("devices", [])
            for node in caps.gpu.device_nodes:
                if node not in devices:
                    devices.append(node)

        return service

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Probe the ML /ping endpoint inside the container."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        if not cfg.category_enabled("cloud"):
            return [VerifyCheck("immich-machine-learning", "ping", True, "cloud not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("immich-machine-learning", "ping", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "immich-machine-learning", root):
            return [VerifyCheck("immich-machine-learning", "ping", False, "container missing")]

        rc, body = docker_curl(
            cfg,
            vm_ip,
            "immich-machine-learning",
            "http://localhost:3003/ping",
            root=root,
            timeout=10,
        )
        ok = rc == 0
        return [
            VerifyCheck(
                "immich-machine-learning",
                "ping",
                ok,
                "/ping ok" if ok else (body or "ping failed")[:120],
            )
        ]


def _safe_load_capabilities(cfg: Config | None):
    """Load capabilities best-effort; return None on any failure.

    The plugin may be invoked during 'generate' on the controller (where no
    GPU is present anyway) — in that case load_capabilities returns the local
    snapshot (has_gpu=False) and the CPU path wins. A hard failure here must
    NOT break generate, so we catch everything and fall back to CPU-only.
    """
    try:
        from toolkit.core.capabilities import load_capabilities

        node = "local" if cfg is None else ImmichMachineLearningPlugin().runtime_node(cfg)
        return load_capabilities(vm=node)
    except Exception:
        return None
