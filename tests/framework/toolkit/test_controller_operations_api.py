from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import toolkit.controller.operations_api as operations_api
from toolkit.controller.operations_api import read_operations_view
from toolkit.controller.read_models import ManagedHostsView, UpdateOperationsView
from toolkit.core.config.config import Config, ServicesConfig, save_config
from toolkit.core.config.storage import config_path


def test_operations_cold_reads_run_in_parallel(monkeypatch, tmp_path: Path) -> None:
    save_config(
        Config(
            domain="example.test",
            services=ServicesConfig(
                management=True, media=False, cloud=False, notifications=False, email=False, security=False
            ),
            proxmox={"provision_machines": False},
        ),
        config_path(tmp_path),
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def slow(value):
        def run(*_args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.08)
            with lock:
                active -= 1
            return value

        return run

    monkeypatch.setattr("toolkit.controller.operations_api._backup_status", slow((None, "", [])))
    monkeypatch.setattr("toolkit.controller.operations_api._dumps", slow([]))
    monkeypatch.setattr(
        "toolkit.controller.operations_api.read_managed_hosts_view",
        slow(ManagedHostsView(revision="0" * 64, hosts=[], service_choices=[])),
    )
    monkeypatch.setattr(
        "toolkit.controller.operations_api._updates_status",
        slow(UpdateOperationsView(available=True, reason="ok")),
    )
    started = time.monotonic()
    read_operations_view(tmp_path)
    elapsed = time.monotonic() - started
    assert peak >= 2
    assert elapsed < 0.25


def test_operations_overlapping_reads_coalesce_to_one_bounded_snapshot(monkeypatch, tmp_path: Path) -> None:
    save_config(Config(domain="example.test", proxmox={"provision_machines": False}), config_path(tmp_path))
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def slow(value):
        def run(*_args):
            nonlocal calls
            with lock:
                calls += 1
                if calls == 4:
                    started.set()
            release.wait(2)
            return value

        return run

    monkeypatch.setattr(operations_api, "_backup_status", slow((None, "", [])))
    monkeypatch.setattr(operations_api, "_dumps", slow([]))
    monkeypatch.setattr(
        operations_api,
        "read_managed_hosts_view",
        slow(ManagedHostsView(revision="0" * 64, hosts=[], service_choices=[])),
    )
    monkeypatch.setattr(
        operations_api,
        "_updates_status",
        slow(UpdateOperationsView(available=True, reason="ok")),
    )
    with ThreadPoolExecutor(max_workers=6) as callers:
        futures = [callers.submit(read_operations_view, tmp_path) for _ in range(6)]
        assert started.wait(1)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert read_operations_view(tmp_path).updates.available is True
    assert calls == 4
    assert all(result.updates.available for result in results)


def test_operations_probe_failure_is_visible_and_rate_limited(monkeypatch, tmp_path: Path) -> None:
    save_config(Config(domain="example.test", proxmox={"provision_machines": False}), config_path(tmp_path))
    calls = 0

    def fail(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("private probe detail")

    monkeypatch.setattr(operations_api, "_build_operations_view", fail)
    failed = read_operations_view(tmp_path)
    cached = read_operations_view(tmp_path)

    assert calls == 1
    assert failed.updates.available is False
    assert failed.updates.reason == "Operational inventory refresh failed; retrying shortly"
    assert cached.updates.reason == failed.updates.reason
    assert "private probe detail" not in failed.model_dump_json()
