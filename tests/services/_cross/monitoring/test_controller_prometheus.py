from __future__ import annotations

import json
from pathlib import Path

from toolkit.controller.prometheus_api import RECORD_SEPARATOR, read_service_metric_history, read_service_metrics
from toolkit.core.config.config import Config


def _result(value: float) -> str:
    return json.dumps({"status": "success", "data": {"result": [{"value": [1, str(value)]}]}})


def _range_result(values: list[tuple[int, float]]) -> str:
    samples = [[timestamp, str(value)] for timestamp, value in values]
    return json.dumps({"status": "success", "data": {"result": [{"values": samples}]}})


def test_container_metrics_use_one_bounded_server_owned_query_batch(monkeypatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def run(_root, _cfg, queries):
        captured.append(queries)
        return RECORD_SEPARATOR.join(
            [
                _result(12.3456),
                _result(384.5),
                _result(100),
                _result(2),
                _result(1.5),
                _result(0.75),
                _result(0.25),
                _result(0.5),
                _result(3600),
            ]
        )

    monkeypatch.setattr("toolkit.controller.prometheus_api.run_prometheus_queries", run)

    metrics = read_service_metrics(tmp_path, Config(), "media-cache", use_cache=False)

    assert metrics == {
        "cpu_percent": 12.35,
        "memory_megabytes": 384.5,
        "available_percent": 100.0,
        "restart_attempts": 2.0,
        "network_receive_mbps": 1.5,
        "network_transmit_mbps": 0.75,
        "disk_read_mbps": 0.25,
        "disk_write_mbps": 0.5,
        "uptime_seconds": 3600.0,
    }
    assert len(captured) == 1
    assert len(captured[0]) == 9
    assert all("media-cache" in query for query in captured[0])


def test_container_metrics_fail_closed_on_malformed_prometheus_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "toolkit.controller.prometheus_api.run_prometheus_queries",
        lambda *_args: "{}" + RECORD_SEPARATOR + "not-json",
    )

    assert read_service_metrics(tmp_path, Config(), "grafana", use_cache=False) == {}


def test_service_metrics_batch_trusted_manifest_queries_with_builtins(monkeypatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def run(_root, _cfg, queries):
        captured.append(queries)
        return RECORD_SEPARATOR.join(
            [_result(1), _result(2), _result(100), _result(0), *[_result(0) for _ in range(5)], _result(47.5)]
        )

    monkeypatch.setattr("toolkit.controller.prometheus_api.run_prometheus_queries", run)

    metrics = read_service_metrics(
        tmp_path,
        Config(),
        "media-cache",
        manifest_queries={"queue_depth": "sum(media_cache_queue_depth)"},
        use_cache=False,
    )

    assert metrics["queue_depth"] == 47.5
    assert len(captured) == 1
    assert len(captured[0]) == 10


def test_service_metric_history_is_bounded_and_parsed(monkeypatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def run(_root, _cfg, urls):
        captured.append(urls)
        cpu = [(1_700_000_000 + index * 60, index * 2.5) for index in range(3)]
        memory = [(1_700_000_000 + index * 60, 256 + index * 10) for index in range(3)]
        return RECORD_SEPARATOR.join([_range_result(cpu), _range_result(memory)])

    monkeypatch.setattr("toolkit.controller.prometheus_api.run_prometheus_urls", run)

    history = read_service_metric_history(tmp_path, Config(), "romm", use_cache=False)

    assert history["cpu_percent"] == [
        (1_700_000_000_000, 0.0),
        (1_700_000_060_000, 2.5),
        (1_700_000_120_000, 5.0),
    ]
    assert history["memory_megabytes"][-1] == (1_700_000_120_000, 276.0)
    assert len(captured) == 1
    assert len(captured[0]) == 2
    assert all("query_range" in url and "romm" in url for url in captured[0])


def test_metrics_and_history_share_one_prometheus_batch(monkeypatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def run(_root, _cfg, urls):
        captured.append(urls)
        return RECORD_SEPARATOR.join(
            [
                *[_result(float(index)) for index in range(9)],
                _range_result([(1_700_000_000, 10.0)]),
                _range_result([(1_700_000_000, 256.0)]),
            ]
        )

    monkeypatch.setattr("toolkit.controller.prometheus_api.run_prometheus_urls", run)

    metrics = read_service_metrics(tmp_path, Config(), "media-cache", include_history=True)
    history = read_service_metric_history(tmp_path, Config(), "media-cache")

    assert metrics["cpu_percent"] == 0.0
    assert history == {
        "cpu_percent": [(1_700_000_000_000, 10.0)],
        "memory_megabytes": [(1_700_000_000_000, 256.0)],
    }
    assert len(captured) == 1
    assert len(captured[0]) == 11
    assert sum("query_range" in url for url in captured[0]) == 2


def test_metric_cache_is_scoped_to_configuration(monkeypatch, tmp_path: Path) -> None:
    calls: list[Config] = []

    def run(_root, cfg, _queries):
        calls.append(cfg)
        return RECORD_SEPARATOR.join([_result(float(len(calls)))] * 9)

    monkeypatch.setattr("toolkit.controller.prometheus_api.run_prometheus_queries", run)

    first = read_service_metrics(tmp_path, Config(domain="first.example"), "media-cache")
    second = read_service_metrics(tmp_path, Config(domain="second.example"), "media-cache")

    assert first["cpu_percent"] == 1.0
    assert second["cpu_percent"] == 2.0
    assert len(calls) == 2
