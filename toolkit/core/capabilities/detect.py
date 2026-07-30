"""Single canonical capability detector.

Owns ``GpuCapabilities`` / ``ServerCapabilities`` / ``detect_gpu_local`` /
``detect_gpu_for_vm`` / ``detect_server_capabilities`` / ``detect_capabilities``,
plus the AES-NI / disk-type / CPU-model probes.

The previously-scattered GPU detectors elsewhere now delegate here:

- ``toolkit.core.infra.autodetect.detect_hw_transcoding`` delegates to
  :func:`detect_capabilities`.
- ``toolkit.core.ops.health_report._detect_gpu`` delegates to
  :func:`detect_capabilities`.

This collapses the three divergent detectors into one canonical path so that
``vainfo`` vs ``/dev/dri``-glob checks can no longer disagree on the same host.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.infra.autodetect import detect_hw_transcoding
from toolkit.core.infra.host_capacity import HostCapacity, detect_host_capacity, detect_lxc_capacity

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

HwBackend = str  # none | vaapi | nvidia


@dataclass(frozen=True, slots=True)
class GpuCapabilities:
    backend: HwBackend
    name: str = ""
    vram_mb: int | None = None
    device_nodes: tuple[str, ...] = ()
    source: str = "local"

    @property
    def has_gpu_transcode(self) -> bool:
        return self.backend in ("nvidia", "vaapi")


@dataclass(frozen=True, slots=True)
class ServerCapabilities:
    host: HostCapacity
    gpu: GpuCapabilities
    vm: str = "local"
    has_aes_ni: bool = False
    disk_type: str = "unknown"  # ssd | hdd | unknown
    cpu_model: str = ""
    detected_at: str = ""

    @property
    def has_gpu(self) -> bool:
        return self.gpu.backend != "none"

    @property
    def gpu_vendor(self) -> str:
        """Normalize the GPU backend to a vendor tag.

        ``nvidia`` is unambiguous. ``vaapi`` implies an Intel or AMD iGPU — the
        existing detectors at F1 don't split that further, so we report the
        conservative ``intel-or-amd`` bucket (Phase N's GPU-adaptive Immich ML
        can refine this if it ever needs vendor-specific runtime selection).
        """
        backend = self.gpu.backend
        if backend == "nvidia":
            return "nvidia"
        if backend == "vaapi":
            return "intel-or-amd"
        return "none"

    @property
    def gpu_vram_mb(self) -> int | None:
        return self.gpu.vram_mb

    @property
    def total_ram_mb(self) -> int:
        return self.host.mem_total_mb

    def to_dict(self) -> dict:
        return {
            "vm": self.vm,
            "host": asdict(self.host),
            "gpu": asdict(self.gpu),
            "has_gpu": self.has_gpu,
            "gpu_vendor": self.gpu_vendor,
            "gpu_vram_mb": self.gpu_vram_mb,
            "has_aes_ni": self.has_aes_ni,
            "disk_type": self.disk_type,
            "cpu_model": self.cpu_model,
            "total_ram_mb": self.total_ram_mb,
            "detected_at": self.detected_at,
        }


def _gpu_probe_shell() -> str:
    return (
        "if [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ]; then "
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null "
        "|| echo 'nvidia|NVIDIA GPU|0'; "
        "elif [ -e /dev/dri/renderD128 ] || [ -e /dev/dri/card0 ]; then "
        "echo 'vaapi|VAAPI|0'; "
        "else echo 'none||0'; fi"
    )


def _parse_gpu_probe(raw: str, *, source: str) -> GpuCapabilities:
    line = (raw or "").strip().splitlines()[0] if raw else ""
    if not line or line.startswith("none"):
        return GpuCapabilities(backend="none", source=source)
    parts = line.split("|", 2)
    backend = parts[0].strip().lower() if parts else "none"
    name = parts[1].strip() if len(parts) > 1 else ""
    vram_mb: int | None = None
    if len(parts) > 2 and parts[2].strip().isdigit():
        # nvidia-smi reports MiB
        vram_mb = int(parts[2].strip())
    nodes: list[str] = []
    if backend == "nvidia":
        nodes = ["/dev/nvidia0", "/dev/nvidiactl"]
    elif backend == "vaapi":
        nodes = ["/dev/dri"]
    return GpuCapabilities(
        backend=backend if backend in ("nvidia", "vaapi") else "none",
        name=name,
        vram_mb=vram_mb,
        device_nodes=tuple(nodes),
        source=source,
    )


def detect_gpu_local() -> GpuCapabilities:
    backend = detect_hw_transcoding()
    name = ""
    if backend == "nvidia":
        try:
            import subprocess

            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0:
                name = out.stdout.strip().splitlines()[0]
        except (FileNotFoundError, OSError):
            name = "NVIDIA GPU"
    elif backend == "vaapi":
        name = "VAAPI (/dev/dri)"
    return _parse_gpu_probe(f"{backend}|{name}|0", source="local")


def detect_gpu_for_vm(
    cfg: Config,
    vm: str,
    *,
    root: Path | None = None,
    fast: bool = True,
) -> GpuCapabilities:
    """Probe GPU on a VM via SSH (media LXC) or locally."""
    from toolkit.core.ansible.ansible_inventory import resolve_node_host_ip
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    if vm == "local" or not cfg.is_multi_node:
        return detect_gpu_local()

    ip = resolve_node_host_ip(root or Path.cwd(), vm, cfg) if root else None
    ip = ip or cfg.node_ip(vm)
    timeout = 10 if fast else 25
    rc, out, _ = ssh_run_on_vm(cfg, ip, _gpu_probe_shell(), root=root, timeout=timeout, retries=1 if fast else 2)
    if rc == 0 and out.strip():
        return _parse_gpu_probe(out, source=f"lxc:{vm}")
    return GpuCapabilities(backend="none", source=f"lxc:{vm}:unreachable")


def detect_server_capabilities(
    cfg: Config | None = None,
    *,
    vm: str | None = None,
    root: Path | None = None,
    fast: bool = True,
) -> ServerCapabilities:
    """Combined host and GPU snapshot for a concrete machine or local host."""
    if cfg is None:
        from toolkit.core.infra.host_capacity import _capacity_from_raw, _cpu_cores, _read_loadavg, _read_mem_total_kb

        host = _capacity_from_raw(_cpu_cores(), _read_mem_total_kb(), _read_loadavg(), source="local")
        gpu = detect_gpu_local()
        return ServerCapabilities(host=host, gpu=gpu, vm=vm or "local")

    node = vm or cfg.control_node

    if cfg.is_multi_node:
        lxc = detect_lxc_capacity(
            cfg,
            root,
            max_hosts=1,
            nodes=(node,),
            retries=1 if fast else 2,
        )
        if lxc is not None:
            from toolkit.core.infra.host_capacity import _capacity_from_raw

            host = _capacity_from_raw(*lxc, source=f"guest:{node}")
        else:
            host = detect_host_capacity(cfg=cfg, root=root, fast=fast)
    else:
        host = detect_host_capacity(cfg=cfg, root=root, fast=fast)

    gpu = detect_gpu_for_vm(cfg, node, root=root, fast=fast)
    return ServerCapabilities(host=host, gpu=gpu, vm=node)


def _detect_aes_ni() -> bool:
    """Return True if /proc/cpuinfo advertises AES-NI support.

    Tokenizes the flags line to avoid matching substrings like ``paes``.
    Best-effort: any read failure returns False.
    """
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return False
    # Normalize whitespace and tokenize per-CPU-block; detect a standalone "aes" flag.
    tokens = text.replace("\t", " ").split()
    return "aes" in tokens


def _detect_disk_type() -> str:
    """Classify the primary disk(s): 'ssd' (all ROTA=0), 'hdd' (any ROTA=1), else 'unknown'.

    Uses ``lsblk -d -b -o NAME,ROTA``. A mixed rotational set is reported as
    'hdd' to stay conservative for workload-planning decisions.
    """
    try:
        result = subprocess.run(
            ["lsblk", "-d", "-b", "-o", "NAME,ROTA"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # best-effort probe: lsblk absent, permission denied, etc.
        return "unknown"
    rows = [ln for ln in result.stdout.splitlines()[1:] if ln.strip()]
    rotas: list[str] = []
    for ln in rows:
        parts = ln.split()
        if len(parts) >= 2 and parts[1].isdigit():
            rotas.append(parts[1])
    if not rotas:
        return "unknown"
    return "ssd" if all(r == "0" for r in rotas) else "hdd"


def _detect_cpu_model() -> str:
    """Return the CPU model name from ``lscpu`` (best-effort, '' on failure)."""
    try:
        result = subprocess.run(
            ["lscpu"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # best-effort probe: lscpu absent, permission denied, etc.
        return ""
    for ln in result.stdout.splitlines():
        low = ln.lower()
        if low.startswith("model name:"):
            return ln.split(":", 1)[1].strip()
    return ""


def detect_capabilities(vm: str = "local", *, force_refresh: bool = False) -> ServerCapabilities:
    """Detect local (or per-VM) capabilities without a config object.

    Lightweight form used by :func:`toolkit.core.capabilities.store.load_capabilities`
    and by callers that only need the local snapshot. Populates the Host + GPU
    snapshot and the AES-NI / disk-type / CPU-model probes.

    NOTE: AES-NI / disk-type / CPU-model probes read the *local* /proc and
    ``lsblk``/``lscpu`` output. When ``vm != "local"`` these fields reflect the
    controller, not the guest — the per-VM SSH probe path
    (:func:`detect_gpu_for_vm`) only covers GPU today; full per-VM AES-NI/disk
    detection is a Phase-N enhancement when GPU-adaptive Immich ML needs it.
    """
    host = detect_host_capacity()
    gpu = detect_gpu_local()
    return ServerCapabilities(
        host=host,
        gpu=gpu,
        vm=vm,
        has_aes_ni=_detect_aes_ni(),
        disk_type=_detect_disk_type(),
        cpu_model=_detect_cpu_model(),
        detected_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


__all__ = [
    "GpuCapabilities",
    "ServerCapabilities",
    "detect_capabilities",
    "detect_gpu_for_vm",
    "detect_gpu_local",
    "detect_server_capabilities",
]
