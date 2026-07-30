"""Bounded controller-owned Prometheus query execution and service telemetry."""

from __future__ import annotations

import json
import math
import shlex
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import Config

RECORD_SEPARATOR = "\x1e"
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_QUERIES = 24
_MAX_QUERY_LENGTH = 2_000
_CACHE_TTL_SECONDS = 10.0
_CACHE_LIMIT = 256
_cache: dict[tuple[Path, str, tuple[tuple[str, str], ...]], tuple[float, dict[str, float]]] = {}
_history_cache: dict[tuple[Path, str], tuple[float, dict[str, list[tuple[int, float]]]]] = {}
_cache_lock = threading.Lock()


def _query_urls(queries: list[str]) -> list[str]:
    if not queries or len(queries) > _MAX_QUERIES:
        raise ValueError("Prometheus query batch size is invalid")
    if any(not query or len(query) > _MAX_QUERY_LENGTH for query in queries):
        raise ValueError("Prometheus query is invalid")
    return [f"http://127.0.0.1:9090/api/v1/query?query={urllib.parse.quote(query, safe='')}" for query in queries]


def _metric_command(container: str, urls: list[str]) -> list[str]:
    script = "for url do /bin/busybox wget -qO- \"$url\" || printf '{}'; printf '\\036'; done"
    return ["docker", "exec", container, "/bin/busybox", "sh", "-c", script, "controller-metrics", *urls]


def run_prometheus_queries(root: Path, cfg: Config, queries: list[str]) -> str:
    """Execute one bounded batch inside Prometheus and return separated JSON payloads."""
    return run_prometheus_urls(root, cfg, _query_urls(queries))


def run_prometheus_urls(root: Path, cfg: Config, urls: list[str]) -> str:
    """Execute a bounded batch of controller-built local Prometheus URLs."""
    if not urls or len(urls) > _MAX_QUERIES:
        raise ValueError("Prometheus URL batch size is invalid")
    prefix = "http://127.0.0.1:9090/api/v1/"
    if any(not url.startswith(prefix) or len(url) > 8_192 for url in urls):
        raise ValueError("Prometheus URL is invalid")
    from toolkit.core.manifest.catalog import load_service_catalog

    metrics_service = load_service_catalog().require_provider("metrics").name
    command = _metric_command(metrics_service, urls)
    if cfg.proxmox.provision_machines:
        from toolkit.core.manifest.placement import service_address

        code, output, _error = ssh_run_on_vm(
            cfg,
            service_address(cfg, metrics_service),
            " ".join(shlex.quote(part) for part in command),
            root=root,
            timeout=20,
            retries=1,
        )
        return output[:_MAX_OUTPUT_BYTES] if code == 0 else ""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout[:_MAX_OUTPUT_BYTES] if result.returncode == 0 else ""


def _instant_number(payload: object) -> float | None:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return None
    value = result[0].get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        number = float(value[1])
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _container_queries(container: str) -> list[str]:
    selector = f'name="{container}"'
    return [
        f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m])) * 100",
        f"max(container_memory_working_set_bytes{{{selector}}}) / 1024 / 1024",
        f"max((time() - container_last_seen{{{selector}}}) < bool 60) * 100",
        f'max(watchdog_restart_attempts_total{{container="{container}"}}) or vector(0)',
        f"sum(rate(container_network_receive_bytes_total{{{selector}}}[5m])) * 8 / 1000000",
        f"sum(rate(container_network_transmit_bytes_total{{{selector}}}[5m])) * 8 / 1000000",
        f"sum(rate(container_fs_reads_bytes_total{{{selector}}}[5m])) * 8 / 1000000",
        f"sum(rate(container_fs_writes_bytes_total{{{selector}}}[5m])) * 8 / 1000000",
        f"max(time() - container_start_time_seconds{{{selector}}})",
    ]


def _validate_container(container: str) -> None:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    if not container or len(container) > 63 or any(character not in allowed for character in container):
        raise ValueError("container name is invalid")


def read_service_metrics(
    root: Path,
    cfg: Config,
    container: str,
    *,
    manifest_queries: dict[str, str] | None = None,
    use_cache: bool = True,
) -> dict[str, float]:
    """Return built-in and trusted-manifest telemetry for one validated service."""
    _validate_container(container)
    extras = dict(manifest_queries or {})
    if len(extras) > 12:
        raise ValueError("service declares too many Prometheus metrics")
    query_items = tuple(extras.items())
    key = (root.resolve(), container, query_items)
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None and now - cached[0] <= _CACHE_TTL_SECONDS:
                return dict(cached[1])

    output = run_prometheus_queries(root, cfg, [*_container_queries(container), *extras.values()])
    parts = output.split(RECORD_SEPARATOR)
    names = (
        "cpu_percent",
        "memory_megabytes",
        "available_percent",
        "restart_attempts",
        "network_receive_mbps",
        "network_transmit_mbps",
        "disk_read_mbps",
        "disk_write_mbps",
        "uptime_seconds",
        *extras.keys(),
    )
    metrics: dict[str, float] = {}
    for name, part in zip(names, parts[: len(names)], strict=False):
        try:
            payload = json.loads(part)
        except json.JSONDecodeError:
            continue
        value = _instant_number(payload)
        if value is not None and value >= 0:
            metrics[name] = round(value, 2)

    if use_cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), dict(metrics))
            if len(_cache) > _CACHE_LIMIT:
                oldest = min(_cache, key=lambda item: _cache[item][0])
                del _cache[oldest]
    return metrics


def _range_points(payload: object, *, clamp_percent: bool = False) -> list[tuple[int, float]]:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return []
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return []
    values = result[0].get("values")
    if not isinstance(values, list):
        return []
    points: list[tuple[int, float]] = []
    for pair in values[:120]:
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        try:
            timestamp = float(pair[0])
            value = float(pair[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
            continue
        if clamp_percent:
            value = min(100.0, value)
        points.append((int(timestamp * 1000), round(value, 2)))
    return points


def read_service_metric_history(
    root: Path,
    cfg: Config,
    container: str,
    *,
    use_cache: bool = True,
) -> dict[str, list[tuple[int, float]]]:
    """Return bounded one-hour CPU and memory series for one managed container."""
    _validate_container(container)
    key = (root.resolve(), container)
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            cached = _history_cache.get(key)
            if cached is not None and now - cached[0] <= _CACHE_TTL_SECONDS:
                return {name: list(points) for name, points in cached[1].items()}

    selector = f'name="{container}"'
    queries = [
        f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m])) * 100",
        f"max(container_memory_working_set_bytes{{{selector}}}) / 1024 / 1024",
    ]
    end = int(time.time())
    urls = [
        "http://127.0.0.1:9090/api/v1/query_range"
        f"?query={urllib.parse.quote(query, safe='')}&start={end - 3600}&end={end}&step=60"
        for query in queries
    ]
    output = run_prometheus_urls(root, cfg, urls)
    parts = output.split(RECORD_SEPARATOR)
    payloads: list[object] = []
    for part in parts[:2]:
        try:
            payloads.append(json.loads(part))
        except json.JSONDecodeError:
            payloads.append({})
    while len(payloads) < 2:
        payloads.append({})
    history = {
        "cpu_percent": _range_points(payloads[0], clamp_percent=True),
        "memory_megabytes": _range_points(payloads[1]),
    }
    if use_cache:
        with _cache_lock:
            _history_cache[key] = (time.monotonic(), {name: list(points) for name, points in history.items()})
            if len(_history_cache) > _CACHE_LIMIT:
                oldest = min(_history_cache, key=lambda item: _history_cache[item][0])
                del _history_cache[oldest]
    return history
