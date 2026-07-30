"""Unit tests for the unified capability detector.

The autouse ``_no_network_probes`` fixture in conftest.py exempts this module
(see ``_PROBE_TEST_MODULES``) so the real detection functions run here —
they're exercised against mocked /proc and subprocess output, never real SSH.
"""

from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from toolkit.core.capabilities.detect import (
    GpuCapabilities,
    ServerCapabilities,
    _detect_aes_ni,
    _detect_cpu_model,
    _detect_disk_type,
    detect_capabilities,
    detect_server_capabilities,
)
from toolkit.core.infra.host_capacity import HostCapacity

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


@pytest.fixture
def mock_host():
    """Stub host-capacity detection so detect_capabilities doesn't hit /proc/SSH."""
    with (
        patch(
            "toolkit.core.capabilities.detect.detect_host_capacity",
            return_value=_TEST_HOST,
        ),
        patch(
            "toolkit.core.capabilities.detect.detect_lxc_capacity",
            return_value=None,
        ),
    ):
        yield


# --- AES-NI -----------------------------------------------------------------


def test_detect_aes_ni_present():
    with patch("pathlib.Path.read_text", return_value="flags\t: fpu vme de aes sse4_2"):
        assert _detect_aes_ni() is True


def test_detect_aes_ni_absent():
    with patch("pathlib.Path.read_text", return_value="flags\t: fpu vme de sse4_2"):
        # tokenize: no standalone 'aes ' / '\naes ' token
        # The text "de aes sse4_2" *does* contain ' aes ' — so AES-NI is detected.
        # Use a cleaner no-aes sample to assert absence.
        pass
    with patch("pathlib.Path.read_text", return_value="flags\t: fpu vme de sse4_2 avx"):
        assert _detect_aes_ni() is False


def test_detect_aes_ni_missing_cpuinfo():
    with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
        assert _detect_aes_ni() is False


# --- disk type --------------------------------------------------------------


def test_detect_disk_type_ssd():
    sample = "NAME ROTA\nsda 0\nsdb 0\n"
    with patch("subprocess.run", return_value=CompletedProcess(args=[], returncode=0, stdout=sample, stderr="")):
        assert _detect_disk_type() == "ssd"


def test_detect_disk_type_hdd():
    sample = "NAME ROTA\nsda 1\n"
    with patch("subprocess.run", return_value=CompletedProcess(args=[], returncode=0, stdout=sample, stderr="")):
        assert _detect_disk_type() == "hdd"


def test_detect_disk_type_mixed_is_hdd():
    # Mixed ROTA → rotational present → classify as hdd (conservative).
    sample = "NAME ROTA\nsda 0\nsdb 1\n"
    with patch("subprocess.run", return_value=CompletedProcess(args=[], returncode=0, stdout=sample, stderr="")):
        assert _detect_disk_type() == "hdd"


def test_detect_disk_type_failure():
    with patch("subprocess.run", side_effect=Exception("no lsblk")):
        assert _detect_disk_type() == "unknown"


# --- cpu model --------------------------------------------------------------


def test_detect_cpu_model():
    out = "Architecture: x86_64\nModel name: Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz\n"
    with patch("subprocess.run", return_value=CompletedProcess(args=[], returncode=0, stdout=out, stderr="")):
        assert _detect_cpu_model() == "Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz"


def test_detect_cpu_model_missing():
    with patch(
        "subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="Architecture: x86_64\n", stderr=""),
    ):
        assert _detect_cpu_model() == ""


def test_detect_cpu_model_failure():
    with patch("subprocess.run", side_effect=Exception("no lscpu")):
        assert _detect_cpu_model() == ""


# --- detect_capabilities (integration of the probes) -----------------------


def test_detect_capabilities_populates_new_fields(mock_host):
    gpu = GpuCapabilities(backend="nvidia", name="RTX 4090", vram_mb=24576, source="local")
    with (
        patch("toolkit.core.capabilities.detect.detect_gpu_local", return_value=gpu),
        patch("toolkit.core.capabilities.detect._detect_aes_ni", return_value=True),
        patch("toolkit.core.capabilities.detect._detect_disk_type", return_value="ssd"),
        patch("toolkit.core.capabilities.detect._detect_cpu_model", return_value="i7-10700K"),
    ):
        caps = detect_capabilities(vm="local")
    assert caps.has_aes_ni is True
    assert caps.disk_type == "ssd"
    assert caps.cpu_model == "i7-10700K"
    assert caps.total_ram_mb == 32768
    assert caps.gpu.vram_mb == 24576
    assert caps.gpu_vendor == "nvidia"
    assert caps.has_gpu is True
    assert caps.detected_at  # ISO 8601 timestamp populated


def test_detect_capabilities_no_gpu(mock_host):
    none_gpu = GpuCapabilities(backend="none", source="local")
    with (
        patch("toolkit.core.capabilities.detect.detect_gpu_local", return_value=none_gpu),
        patch("toolkit.core.capabilities.detect._detect_aes_ni", return_value=False),
        patch("toolkit.core.capabilities.detect._detect_disk_type", return_value="hdd"),
        patch("toolkit.core.capabilities.detect._detect_cpu_model", return_value="ARM Cortex"),
    ):
        caps = detect_capabilities(vm="infra")
    assert caps.has_gpu is False
    assert caps.gpu_vendor == "none"
    assert caps.gpu_vram_mb is None
    assert caps.disk_type == "hdd"


def test_unspecified_server_capability_probe_is_local() -> None:
    with (
        patch("toolkit.core.infra.host_capacity._cpu_cores", return_value=8),
        patch("toolkit.core.infra.host_capacity._read_mem_total_kb", return_value=16_777_216),
        patch("toolkit.core.infra.host_capacity._read_loadavg", return_value=0.5),
        patch(
            "toolkit.core.capabilities.detect.detect_gpu_local",
            return_value=GpuCapabilities(backend="none", source="local"),
        ),
    ):
        caps = detect_server_capabilities()

    assert caps.vm == "local"


# --- ServerCapabilities derived properties + to_dict roundtrip ---------------


def test_server_capabilities_to_dict_includes_new_fields():
    caps = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="vaapi", name="iGPU", vram_mb=0, source="local"),
        vm="media",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="i7",
        detected_at="2026-06-25T00:00:00+00:00",
    )
    d = caps.to_dict()
    assert d["has_gpu"] is True
    assert d["gpu_vendor"] in ("intel-or-amd", "intel", "amd")  # implementation detail
    assert d["gpu_vram_mb"] == 0
    assert d["has_aes_ni"] is True
    assert d["disk_type"] == "ssd"
    assert d["cpu_model"] == "i7"
    assert d["total_ram_mb"] == 32768


def test_server_capabilities_gpu_vendor_none():
    caps = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="none", source="local"),
        vm="infra",
    )
    assert caps.gpu_vendor == "none"
    assert caps.has_gpu is False
