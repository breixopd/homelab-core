"""Tests for capability persistence + 1h TTL cache in toolkit.core.capabilities.store.

The module is in _PROBE_TEST_MODULES so the autouse stub doesn't override our
explicit mock of ``detect_capabilities``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from toolkit.core.capabilities import GpuCapabilities, ServerCapabilities, load_capabilities
from toolkit.core.infra.host_capacity import HostCapacity

_TEST_HOST = HostCapacity(
    cpu_cores=4,
    mem_total_mb=8192,
    load_1m=0.5,
    wave_timeout_s=180,
    inter_wave_sleep_s=5,
    max_pull_parallel=2,
    load_threshold=8.0,
    source="local",
)
_TEST_GPU = GpuCapabilities(backend="none", source="test")
_TEST_CAPS = ServerCapabilities(
    host=_TEST_HOST,
    gpu=_TEST_GPU,
    vm="local",
    has_aes_ni=True,
    disk_type="ssd",
    cpu_model="Test",
    detected_at="2026-01-01T00:00:00+00:00",
)


def test_load_capabilities_writes_json_cache(tmp_path: Path):
    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=_TEST_CAPS):
        caps = load_capabilities(vm="local", root=tmp_path)
    cache_dir = tmp_path / "generated"
    cached_files = list(cache_dir.glob("server-capabilities-*.json"))
    assert cached_files, f"expected cache file under {cache_dir}, found {cached_files}"
    data = json.loads(cached_files[0].read_text())
    assert data["vm"] == "local"
    assert data["has_aes_ni"] is True
    assert caps.vm == "local"
    assert caps.has_aes_ni is True


def test_load_capabilities_returns_cached_within_ttl(tmp_path: Path):
    cache_file = tmp_path / "generated" / "server-capabilities-local.json"
    cache_file.parent.mkdir(parents=True)
    cached = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="nvidia", name="RTX", source="cache"),
        vm="local",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="Cached",
        detected_at="2026-01-01T00:00:00+00:00",
    )
    cache_file.write_text(json.dumps(cached.to_dict()))

    calls = []

    def fake_detect(**kw):
        calls.append(kw)
        return _TEST_CAPS

    with patch("toolkit.core.capabilities.store.detect_capabilities", side_effect=fake_detect):
        result = load_capabilities(vm="local", root=tmp_path)
    assert result.gpu.name == "RTX"  # from cache
    assert calls == []  # detection NOT called


def test_force_refresh_bypasses_cache(tmp_path: Path):
    cache_file = tmp_path / "generated" / "server-capabilities-local.json"
    cache_file.parent.mkdir(parents=True)
    cached = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="nvidia", name="Old", source="cache"),
        vm="local",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="Old",
        detected_at="2026-01-01T00:00:00+00:00",
    )
    cache_file.write_text(json.dumps(cached.to_dict()))
    # Make cache "fresh".
    now = time.time()
    os.utime(cache_file, (now, now))

    fresh = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="vaapi", name="New", source="fresh"),
        vm="local",
        has_aes_ni=True,
        disk_type="nvme",
        cpu_model="New",
        detected_at="2026-06-25T00:00:00+00:00",
    )
    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=fresh):
        result = load_capabilities(vm="local", root=tmp_path, force_refresh=True)
    assert result.gpu.name == "New"


def test_stale_cache_past_ttl_redetects(tmp_path: Path):
    cache_file = tmp_path / "generated" / "server-capabilities-local.json"
    cache_file.parent.mkdir(parents=True)
    cached = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="nvidia", name="Stale", source="cache"),
        vm="local",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="Stale",
        detected_at="2026-01-01T00:00:00+00:00",
    )
    cache_file.write_text(json.dumps(cached.to_dict()))
    # Make the file 2 hours old → past the 1h TTL.
    old = time.time() - (2 * 3600)
    os.utime(cache_file, (old, old))

    fresh = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="vaapi", name="Fresh", source="fresh"),
        vm="local",
        has_aes_ni=True,
        disk_type="nvme",
        cpu_model="Fresh",
        detected_at="2026-06-25T00:00:00+00:00",
    )
    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=fresh):
        result = load_capabilities(vm="local", root=tmp_path)
    assert result.gpu.name == "Fresh"


def test_corrupt_cache_redetects(tmp_path: Path):
    cache_file = tmp_path / "generated" / "server-capabilities-local.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("{ not valid json")  # corrupt
    now = time.time()
    os.utime(cache_file, (now, now))

    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=_TEST_CAPS):
        result = load_capabilities(vm="local", root=tmp_path)
    assert result.vm == "local"
    # Cache file rewritten with valid JSON.
    data = json.loads(cache_file.read_text())
    assert data["vm"] == "local"


def test_custom_ttl_via_env(tmp_path: Path, monkeypatch):
    # Set a 1-second TTL via env var.
    monkeypatch.setenv("HOMELAB_CAPABILITIES_TTL_SECONDS", "1")

    cache_file = tmp_path / "generated" / "server-capabilities-local.json"
    cache_file.parent.mkdir(parents=True)
    cached = ServerCapabilities(
        host=_TEST_HOST,
        gpu=GpuCapabilities(backend="nvidia", name="First", source="cache"),
        vm="local",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="First",
        detected_at="2026-01-01T00:00:00+00:00",
    )
    cache_file.write_text(json.dumps(cached.to_dict()))
    # NOTE: conftest's autouse `_no_network_probes` patches time.sleep to a no-op
    # for ALL tests (not just non-probe modules), so a real sleep here is a
    # no-op. To exercise staleness we instead force the file's mtime into the
    # past directly.
    os.utime(cache_file, (time.time(), time.time()))

    # First call: cache is fresh (age ~0) → uses cache, does NOT detect.
    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=_TEST_CAPS) as m:
        r1 = load_capabilities(vm="local", root=tmp_path)
    assert r1.gpu.name == "First"
    assert m.call_count == 0

    # Age the cache past the 1s TTL by rewinding mtime 2 seconds.
    os.utime(cache_file, (time.time() - 2, time.time() - 2))

    # Second call: stale → MUST redetect.
    with patch("toolkit.core.capabilities.store.detect_capabilities", return_value=_TEST_CAPS) as m2:
        r2 = load_capabilities(vm="local", root=tmp_path)
    assert m2.call_count == 1
    assert r2.cpu_model == "Test"  # fresh detect result, not stale cached "First"
