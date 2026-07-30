"""Unit tests for service automation hooks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from toolkit.core.ops.automation import health_check_logs


def test_health_check_logs_reports_unreachable():
    with patch("toolkit.core.ops.automation.http_reachable", return_value=(False, "timeout")):
        logs = health_check_logs([("svc", "http://svc:8080/")])
    assert len(logs) == 1
    assert "not ready" in logs[0]


def test_resolve_docker_service_url_uses_container_ip():
    from toolkit.core.ops.automation import resolve_docker_service_url

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"homelab":{"IPAddress":"172.20.0.5","NetworkID":"abc"}}',
        )
        url = resolve_docker_service_url("test-service", 80)
    assert url == "http://172.20.0.5:80"


def test_resolve_docker_service_url_avoids_compose_hostname_on_host():
    from toolkit.core.ops.automation import resolve_docker_service_url

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        url = resolve_docker_service_url("test-service", 80)
    assert url == "http://127.0.0.1:80"
    assert "test-service" not in url
