from __future__ import annotations

from toolkit.core.config.config import Config, ExternalHost
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.monitoring import compile_prometheus_targets


def test_scrape_targets_are_owned_by_service_manifests() -> None:
    targets = compile_prometheus_targets(Config())
    by_job = {}
    for target in targets:
        by_job.setdefault(target.job, set()).add((target.target, target.instance))

    assert by_job["prometheus"] == {("10.10.10.10:9090", "infra")}
    assert by_job["node"] == {
        ("10.10.10.10:9100", "infra"),
        ("10.10.10.11:9100", "media"),
        ("10.10.10.12:9100", "apps"),
    }
    assert by_job["cadvisor"] == {
        ("10.10.10.10:8089", "infra"),
        ("10.10.10.11:8088", "media"),
        ("10.10.10.12:8088", "apps"),
    }
    assert by_job["postgres"] == {("10.10.10.10:9187", "infra")}
    assert by_job["redis"] == {("10.10.10.10:9121", "infra")}


def test_external_scrapes_follow_selected_host_integrations() -> None:
    cfg = Config(
        external_hosts=[
            ExternalHost(
                name="edge-01",
                ip="100.64.0.20",
                kind="fleet",
                services=["monitoring-agent"],
            ),
            ExternalHost(name="storage-01", ip="10.0.0.20", services=[]),
        ]
    )

    node_targets = [target for target in compile_prometheus_targets(cfg) if target.job == "node"]

    assert any(target.target == "100.64.0.20:9100" and target.instance == "edge-01" for target in node_targets)
    assert not any(target.instance == "storage-01" for target in node_targets)


def test_metrics_provider_is_selected_by_capability_not_service_name() -> None:
    prometheus = next(manifest for manifest in load_service_catalog().manifests if "metrics" in manifest.provides)
    replacement = prometheus.model_copy(update={"name": "victoria-metrics"})
    catalog = ServiceCatalog((replacement,))

    targets = compile_prometheus_targets(Config(), catalog)

    self_target = next(target for target in targets if target.service == "victoria-metrics")
    assert self_target.target == "10.10.10.10:9090"
