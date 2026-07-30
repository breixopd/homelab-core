from __future__ import annotations

import pytest
from toolkit.core.ops.health_report import (
    ContainerHealthSection,
    HealthReport,
    MaintenanceSection,
    ResourcesSection,
    SecuritySection,
    UpdatesSection,
    _parse_size_to_gb,
    _run_framework_check_script,
)


def test_updates_section_total():
    sec = UpdatesSection(
        python_lock=[{"name": "pydantic"}],
        toolkit_binaries=[{"name": "sops"}, {"name": "age"}],
    )
    assert sec.total == 3
    assert sec.has_updates


def test_container_section_ok():
    healthy = ContainerHealthSection(total=5, healthy=5)
    assert healthy.ok
    unhealthy = ContainerHealthSection(total=2, healthy=1, unhealthy=["postgres"])
    assert not unhealthy.ok


def test_health_report_has_issues_flags_disk_pressure():
    report = HealthReport(
        domain="example.com",
        resources=ResourcesSection(disk_root_percent=95.0),
    )
    assert report.has_issues()


def test_health_report_surfaces_dependency_check_failure() -> None:
    report = HealthReport(updates=UpdatesSection(check_error="registry <unavailable>"))

    assert report.has_issues()
    assert "Dependency check failed: registry <unavailable>" in report.format_text()
    assert "All systems up-to-date" not in report.format_text()
    assert "registry &lt;unavailable&gt;" in report.format_html()
    assert "registry <unavailable>" not in report.format_html()


def test_health_dependency_check_uses_cache_and_preserves_failure(tmp_path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, timeout):
        observed.update(command=command, timeout=timeout)
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "registry unavailable"})()

    monkeypatch.setattr("toolkit.core.ops.health_report._run", fake_run)

    updates, error = _run_framework_check_script(tmp_path)

    assert updates == []
    assert error == "registry unavailable"
    assert "--cache" in observed["command"]


def test_health_report_format_text_includes_sections():
    report = HealthReport(
        domain="example.com",
        updates=UpdatesSection(),
        containers=ContainerHealthSection(total=1, healthy=1),
        resources=ResourcesSection(cpu_cores=4, memory_total_gb=16.0),
    )
    text = report.format_text()
    assert "HEALTH REPORT" in text
    assert "example.com" in text
    assert "CONTAINERS" in text
    assert "All containers healthy" in text


def test_health_report_format_json_roundtrip():
    import json

    report = HealthReport(
        domain="test.local",
        security=SecuritySection(wazuh_status="green", ufw_active=True),
        maintenance=MaintenanceSection(docker_prune_gb=0.5),
    )
    payload = json.loads(report.format_json())
    assert payload["domain"] == "test.local"
    assert payload["security"]["wazuh_status"] == "green"


def test_parse_size_to_gb_health_report():
    assert _parse_size_to_gb("1.5GB") == 1.5
    assert _parse_size_to_gb("1024KB") == pytest.approx(0.001024)
