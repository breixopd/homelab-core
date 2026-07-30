from __future__ import annotations

from toolkit.core.capabilities import GpuCapabilities
from toolkit.core.infra.host_capacity import HostCapacity
from toolkit.services.tdarr.capabilities import recommend_cpu_workers, recommend_gpu_workers


def test_recommend_tdarr_cpu_workers_scales_down_on_small_host():
    cap = HostCapacity(
        cpu_cores=4,
        mem_total_mb=8192,
        load_1m=1.0,
        wave_timeout_s=120,
        inter_wave_sleep_s=4,
        max_pull_parallel=3,
        load_threshold=8.0,
    )
    assert recommend_cpu_workers(cap) == 3


def test_recommend_tdarr_gpu_workers_zero_without_gpu():
    cap = HostCapacity(
        cpu_cores=8,
        mem_total_mb=16384,
        load_1m=1.0,
        wave_timeout_s=120,
        inter_wave_sleep_s=4,
        max_pull_parallel=4,
        load_threshold=16.0,
    )
    gpu = GpuCapabilities(backend="none")
    assert recommend_gpu_workers(cap, gpu) == 0


def test_recommend_tdarr_gpu_workers_one_with_vaapi():
    cap = HostCapacity(
        cpu_cores=8,
        mem_total_mb=16384,
        load_1m=1.0,
        wave_timeout_s=120,
        inter_wave_sleep_s=4,
        max_pull_parallel=4,
        load_threshold=16.0,
    )
    gpu = GpuCapabilities(backend="vaapi", name="VAAPI")
    assert recommend_gpu_workers(cap, gpu) == 1
