"""Grafana provisioning QA helpers."""

from toolkit.services.grafana.bootstrap import grafana_provisioning_ok


def test_grafana_provisioning_ok_healthy():
    logs = ["Grafana: healthy", "Grafana: Prometheus datasource OK", "Grafana: Loki datasource OK"]
    assert grafana_provisioning_ok(logs) is True


def test_grafana_provisioning_ok_unreachable():
    logs = ["Grafana: not reachable (0: connection refused)"]
    assert grafana_provisioning_ok(logs) is False


def test_grafana_provisioning_ok_missing_datasource():
    logs = ["Grafana: healthy", "Grafana: Prometheus datasource missing (file provisioning pending?)"]
    assert grafana_provisioning_ok(logs) is False
