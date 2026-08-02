"""Unit tests for wazuh-indexer and wazuh-dashboard plugin verify()."""

from __future__ import annotations

import json
from base64 import b64decode
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services.sdk.wazuh import WazuhAgentSummary, wazuh_list_agents, wazuh_parse_agent_lines


def _indexer():
    return load_plugin("wazuh-indexer").WazuhIndexerPlugin()


def _dashboard():
    return load_plugin("wazuh-dashboard").WazuhPlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(security=True, cloud=True))


def test_dashboard_post_start_reports_host_manager_and_integration(monkeypatch):
    module = load_plugin("wazuh-dashboard")
    monkeypatch.setattr(module, "_systemd_active", lambda *_a, **_k: True)
    monkeypatch.setattr(module, "_ntfy_integration_installed", lambda: True)
    status = MagicMock(
        returncode=0,
        stdout=("wazuh-db is running\nwazuh-remoted is running\nwazuh-analysisd is running\nwazuh-apid is running\n"),
        stderr="",
    )
    with patch("subprocess.run", return_value=status):
        logs = _dashboard().post_start(_cfg(), {})

    assert logs == [
        "Wazuh -> ntfy integration: installed",
        "Wazuh Manager: service active",
        "Wazuh Manager: core daemons running",
    ]


def test_dashboard_post_start_marks_inactive_manager_critical(monkeypatch):
    module = load_plugin("wazuh-dashboard")
    monkeypatch.setattr(module, "_systemd_active", lambda *_a, **_k: False)
    monkeypatch.setattr(module, "_ntfy_integration_installed", lambda: False)
    logs = _dashboard().post_start(_cfg(), {})

    assert "WARNING: Wazuh Manager: systemd unit not active" in logs


def test_agent_parser_keeps_id_prefixed_records() -> None:
    summary = wazuh_parse_agent_lines(
        "Wazuh agent_control. List of available agents:\n"
        "  ID: 000, Name: manager, IP: 127.0.0.1, Active/Local\n"
        "  ID: 001, Name: apps, IP: any, Active\n"
        "  ID: 002, Name: media, IP: any, Disconnected\n"
    )

    assert summary.total == 2
    assert summary.active == 1


def test_agent_parser_does_not_count_inactive_as_active() -> None:
    summary = wazuh_parse_agent_lines(
        "ID: 001, Name: apps, IP: any, Active\n"
        "ID: 002, Name: old, IP: any, Inactive\n"
        "ID: 003, Name: media, IP: any, Disconnected\n"
    )

    assert summary.total == 3
    assert summary.active == 1


def test_dashboard_management_reports_safe_agent_counts_and_inventory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.services.sdk.wazuh_list_agents",
        lambda *_a, **_k: (
            WazuhAgentSummary(
                total=3,
                active=1,
                lines=[
                    "ID: 001, Name: apps, IP: 10.0.0.5, Active",
                    "ID: 002, Name: media, IP: 10.0.0.6, Disconnected",
                    "ID: 003, Name: retired, IP: any, Inactive",
                ],
            ),
            "",
        ),
    )
    plugin = _dashboard()

    assert plugin.status(_cfg(), {"WAZUH_API_PASSWORD": "must-not-escape"}, tmp_path) == {
        "agents_total": 3,
        "agents_active": 1,
        "agents_disconnected": 1,
    }
    rows = plugin.resources(_cfg(), {"WAZUH_API_PASSWORD": "must-not-escape"}, tmp_path)["agents"]
    assert rows == [
        {"id": "001", "name": "apps", "address": "10.0.0.5", "status": "Active"},
        {"id": "002", "name": "media", "address": "10.0.0.6", "status": "Disconnected"},
        {"id": "003", "name": "retired", "address": "any", "status": "Inactive"},
    ]
    assert "must-not-escape" not in str(rows)


def test_dashboard_management_marks_agent_inventory_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.services.sdk.wazuh_list_agents", lambda *_a, **_k: (None, "manager unavailable"))
    plugin = _dashboard()

    assert plugin.status(_cfg(), {}, tmp_path) == {}
    with pytest.raises(RuntimeError, match="inventory is unavailable"):
        plugin.resources(_cfg(), {}, tmp_path)


def test_wazuh_agent_list_rejects_oversized_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.services.sdk._vmexec.ssh_on_vm",
        lambda *_a, **_k: (0, "x" * (512 * 1024 + 1), ""),
    )

    summary, error = wazuh_list_agents(_cfg(), "10.10.10.10", tmp_path)

    assert summary is None
    assert error == "agent_control output exceeds the safety limit"


def test_wazuh_dashboard_uses_dedicated_upstream_service_accounts() -> None:
    root = Path(__file__).parents[3]
    compose = yaml.safe_load((root / "toolkit/services/wazuh-dashboard/compose.yaml").read_text())
    environment = compose["services"]["wazuh-dashboard"]["environment"]

    assert environment["INDEXER_USERNAME"] == "admin"
    assert environment["DASHBOARD_USERNAME"] == "kibanaserver"
    assert environment["DASHBOARD_PASSWORD"] == "${WAZUH_DASHBOARD_PASSWORD}"
    assert environment["WAZUH_API_URL"] == "https://wazuh-manager:55000"


def test_wazuh_manager_hook_keeps_credentials_out_of_process_arguments() -> None:
    root = Path(__file__).parents[3]
    tasks = yaml.safe_load((root / "toolkit/services/wazuh-indexer/ansible/manager.yml").read_text())
    serialized = str(tasks)

    assert "/security/users/" in serialized
    assert "failed_items" in serialized
    assert "rbac_control" not in serialized
    assert "wazuh_api_previous_password" in serialized
    assert "stdin" in serialized
    assert "no_log" in serialized
    for task in tasks:
        command = task.get("ansible.builtin.command", {})
        if isinstance(command, dict):
            assert "{{ wazuh_api_password }}" not in " ".join(command.get("argv", []))


def test_wazuh_indexer_projects_manager_api_secret_to_ansible() -> None:
    assert _indexer().ansible_secret_variables(
        _cfg(),
        {"WAZUH_API_PASSWORD": "api-secret", "WAZUH_INDEXER_PASSWORD": "index-secret"},
    ) == {
        "wazuh_api_password": "api-secret",
        "wazuh_indexer_password": "index-secret",
    }


def test_wazuh_manager_filebeat_pipeline_is_tls_authenticated_and_secret_safe() -> None:
    root = Path(__file__).parents[3]
    main_tasks = yaml.safe_load((root / "automation/ansible/roles/wazuh_manager/tasks/main.yml").read_text())
    filebeat_tasks = yaml.safe_load((root / "automation/ansible/roles/wazuh_manager/tasks/filebeat.yml").read_text())
    tasks = [*main_tasks, *filebeat_tasks]
    filebeat = (root / "automation/ansible/roles/wazuh_manager/templates/filebeat.yml.j2").read_text()
    ossec = (root / "automation/ansible/roles/wazuh_manager/templates/wazuh-ossec-indexer.xml.j2").read_text()
    serialized = str(tasks)

    assert "filebeat test output" not in serialized  # command argv remains shell-free
    assert "wazuh_filebeat_template_sha256" in serialized
    assert "wazuh_filebeat_module_sha256" in serialized
    assert "wazuh_ca_private_key.stat.exists" in serialized
    assert "filebeat keystore add" not in serialized  # argv is structured, not a shell string
    assert "wazuh_indexer_password" in serialized
    assert "no_log" in serialized
    assert "${password}" in filebeat
    assert "ssl.certificate_authorities" in filebeat
    assert "http://" not in filebeat
    assert "<certificate>" in ossec
    assert "<key>" in ossec
    for task in tasks:
        command = task.get("ansible.builtin.command", {})
        if isinstance(command, dict):
            assert "{{ wazuh_indexer_password }}" not in " ".join(command.get("argv", []))


class TestWazuhIndexerVerify:
    def test_authenticated_probes_use_the_mounted_ca_and_secure_request_transport(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        with (
            patch(
                "toolkit.services.sdk.docker_curl",
                side_effect=[(0, json.dumps({"status": "green"})), (0, "wazuh-alerts-000001\n")],
            ) as request,
            patch(
                "toolkit.services.sdk.docker_exec_on_vm",
                side_effect=AssertionError("raw command transport is forbidden"),
            ),
        ):
            checks = {
                check.check: check
                for check in _indexer().verify(
                    _cfg(), {"WAZUH_INDEXER_PASSWORD": "test-only-secret"}, "10.10.10.10", tmp_path
                )
            }

        assert checks["cluster_health"].passed
        assert checks["alert_indices"].passed
        assert request.call_args_list[0].args[1:4] == (
            "10.10.10.10",
            "wazuh-indexer",
            "https://wazuh.indexer:9200/_cluster/health",
        )
        assert request.call_args_list[0].kwargs["ca_file"] == "/usr/share/wazuh-indexer/config/certs/root-ca.pem"
        authorization = request.call_args_list[0].kwargs["headers"]["Authorization"]
        assert authorization.startswith("Basic ")
        assert b64decode(authorization.removeprefix("Basic ")).decode() == "admin:test-only-secret"

    def test_cluster_health_green(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_request(_cfg, _ip, _container, url, **_kw):
            if "_cluster/health" in url:
                return 0, json.dumps({"status": "green"})
            if "wazuh-alerts" in url:
                return 0, "wazuh-alerts-000001\n"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        checks = {
            c.check: c for c in _indexer().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["cluster_health"].passed
        assert checks["alert_indices"].passed

    def test_alert_indices_are_not_ready_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_request(_cfg, _ip, _container, url, **_kw):
            if "_cluster/health" in url:
                return 0, json.dumps({"status": "green"})
            if "wazuh-alerts" in url:
                return 0, ""
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        checks = {
            c.check: c for c in _indexer().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["alert_indices"].passed is False
        assert checks["alert_indices"].status.value == "not_ready"

    def test_alert_indices_probe_failure_does_not_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_request(_cfg, _ip, _container, url, **_kw):
            if "_cluster/health" in url:
                return 0, json.dumps({"status": "green"})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        checks = {
            c.check: c for c in _indexer().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["alert_indices"].passed is False

    def test_cluster_health_red_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (0, json.dumps({"status": "red"})),
        )
        checks = {
            c.check: c for c in _indexer().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["cluster_health"].passed is False


class TestWazuhDashboardVerify:
    def test_dashboard_and_agents(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_request(_cfg, _ip, _container, url, **_kw):
            if "app/login" in url:
                return 0, "login page"
            if "_cluster/health" in url:
                return 0, json.dumps({"status": "yellow"})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        monkeypatch.setattr(
            "toolkit.services.sdk.wazuh_list_agents",
            lambda *_a, **_k: (WazuhAgentSummary(total=3, active=2, lines=[]), ""),
        )

        checks = {
            c.check: c for c in _dashboard().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert checks["dashboard_api"].passed
        assert checks["indexer_link"].passed
        assert checks["agents"].passed

    def test_agents_fail_when_none_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_request(_cfg, _ip, _container, url, **_kw):
            if "app/login" in url:
                return 0, "login page"
            if "_cluster/health" in url:
                return 0, json.dumps({"status": "green"})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        monkeypatch.setattr(
            "toolkit.services.sdk.wazuh_list_agents",
            lambda *_a, **_k: (WazuhAgentSummary(total=0, active=0, lines=[]), ""),
        )
        checks = {
            c.check: c for c in _dashboard().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }
        assert not checks["agents"].passed
        assert "0/0" in checks["agents"].detail

    def test_agents_fail_when_registered_but_not_enough_are_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda _cfg, _ip, _container, url, **_kw: (
                (0, "login page") if "app/login" in url else (0, json.dumps({"status": "green"}))
            ),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.wazuh_list_agents",
            lambda *_a, **_k: (WazuhAgentSummary(total=3, active=1, lines=[]), ""),
        )

        checks = {
            c.check: c for c in _dashboard().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }

        assert not checks["agents"].passed
        assert "1/3" in checks["agents"].detail

    def test_missing_manager_is_not_treated_as_undeployed_fleet(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda _cfg, _ip, _container, url, **_kw: (
                (0, "login page") if "app/login" in url else (0, json.dumps({"status": "green"}))
            ),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.wazuh_list_agents",
            lambda *_a, **_k: (None, "/var/ossec/bin/agent_control: No such file or directory"),
        )

        checks = {
            c.check: c for c in _dashboard().verify(_cfg(), {"WAZUH_INDEXER_PASSWORD": "pw"}, "10.10.10.10", tmp_path)
        }

        assert not checks["manager"].passed
