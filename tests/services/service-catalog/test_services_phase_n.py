"""Phase N: new services — Uptime Kuma, CrowdSec, GPU-adaptive Immich ML.

Tests:
1. The three new services are discoverable by the plugin loader.
2. GPU-adaptive Immich ML: compose_service() picks the right runtime based on
   load_capabilities() (nvidia → runtime:nvidia, vaapi → /dev/dri, none → CPU).
"""

from __future__ import annotations

from unittest.mock import patch

from toolkit.core.capabilities import GpuCapabilities, ServerCapabilities
from toolkit.core.infra.host_capacity import HostCapacity
from toolkit.services import get_service_plugin

_TEST_HOST = HostCapacity(
    cpu_cores=8,
    mem_total_mb=32768,
    load_1m=0.5,
    wave_timeout_s=300,
    inter_wave_sleep_s=8,
    max_pull_parallel=4,
    load_threshold=8.0,
    source="local",
)


# --- new services are discovered ------------------------------------------


def test_uptime_kuma_plugin_discovered():
    plugin = get_service_plugin("uptime-kuma")
    assert plugin is not None
    assert plugin.category == "notifications"
    assert plugin.placement == "control"


def test_crowdsec_plugin_discovered():
    plugin = get_service_plugin("crowdsec")
    assert plugin is not None
    assert plugin.category == "security"
    assert plugin.placement == "control"


def test_immich_ml_plugin_discovered():
    plugin = get_service_plugin("immich-machine-learning")
    assert plugin is not None
    assert plugin.category == "cloud"
    assert plugin.placement == "apps"


# --- GPU-adaptive Immich ML ------------------------------------------------


def _caps(backend: str) -> ServerCapabilities:
    gpu = GpuCapabilities(
        backend=backend,
        name="RTX" if backend == "nvidia" else "iGPU",
        vram_mb=24576 if backend == "nvidia" else None,
        device_nodes=("/dev/nvidia0", "/dev/nvidiactl") if backend == "nvidia" else ("/dev/dri",),
        source="test",
    )
    return ServerCapabilities(
        host=_TEST_HOST,
        gpu=gpu,
        vm="apps",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="Test",
        detected_at="2026-06-25T00:00:00+00:00",
    )


def test_immich_ml_cpu_only_when_no_gpu():
    plugin = get_service_plugin("immich-machine-learning")
    with (
        patch("toolkit.services.immich-machine-learning.plugin")
        if False
        else patch(
            "toolkit.core.capabilities.load_capabilities",
            return_value=_caps("none"),
        )
    ):
        service = plugin.compose_service()
    assert "runtime" not in service
    env = service.get("environment", {})
    assert "NVIDIA_VISIBLE_DEVICES" not in env
    assert "devices" not in service or service.get("devices") == []


def test_immich_ml_nvidia_runtime_when_gpu_present():
    plugin = get_service_plugin("immich-machine-learning")
    with patch("toolkit.core.capabilities.load_capabilities", return_value=_caps("nvidia")):
        service = plugin.compose_service()
    assert service["runtime"] == "nvidia"
    env = service["environment"]
    assert env["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert env["NVIDIA_DRIVER_CAPABILITIES"] == "compute,utility"


def test_immich_ml_vaapi_mounts_dev_dri():
    plugin = get_service_plugin("immich-machine-learning")
    with patch("toolkit.core.capabilities.load_capabilities", return_value=_caps("vaapi")):
        service = plugin.compose_service()
    assert "runtime" not in service  # vaapi doesn't need the nvidia runtime
    devices = service.get("devices", [])
    assert "/dev/dri" in devices


def test_immich_ml_falls_back_to_cpu_on_detection_failure():
    """If load_capabilities raises, the plugin must NOT break generate — CPU path wins."""
    plugin = get_service_plugin("immich-machine-learning")
    with patch("toolkit.core.capabilities.load_capabilities", side_effect=RuntimeError("no caps")):
        service = plugin.compose_service()
    assert "runtime" not in service
    assert "NVIDIA_VISIBLE_DEVICES" not in service.get("environment", {})
