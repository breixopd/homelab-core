"""Tests for observability stack deep verification."""

from unittest.mock import patch

from toolkit.core.config.config import Config
from toolkit.core.ops.hook_verify import VerifyCheck
from toolkit.core.ops.monitoring_verify import verify_monitoring_stack


def test_verify_monitoring_stack_prometheus_targets(tmp_path):
    cfg = Config()
    targets_json = '{"data":{"activeTargets":[{"health":"up"},{"health":"down","labels":{"job":"node"}}]}}'

    def fake_curl(_cfg, _ip, container, _url, **kwargs):
        if container == "prometheus":
            return 0, targets_json
        if container == "grafana":
            if "contact-points" in _url:
                return 0, '[{"uid":"homelab-ntfy","name":"homelab"}]'
            if "alert-rules" in _url:
                return 0, '[{"uid":"homelab-instance-down","ruleGroup":"homelab-core"}]'
            return 0, '{"status":"OK"}'
        if container == "loki":
            return 0, '{"data":["job","host"]}'
        if container == "komodo-core":
            return 0, "ok"
        return 255, ""

    with patch("toolkit.core.ansible.ansible_ssh.docker_exec_curl", side_effect=fake_curl):
        with patch("toolkit.core.ops.monitoring_verify._verify_prometheus_reload") as fake_reload:
            with patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "", "")):
                fake_reload.return_value = VerifyCheck("prometheus", "reload", True, "reload ok")
                checks = verify_monitoring_stack(cfg, {"GRAFANA_ADMIN_PASSWORD": "x"}, tmp_path)

    by_key = {(c.service, c.check): c for c in checks}
    assert by_key[("prometheus", "targets")].passed is False
    assert "1/2" in by_key[("prometheus", "targets")].detail
    assert by_key[("grafana", "datasource_health_prometheus")].passed is True
    assert by_key[("grafana", "alerting_contact_point")].passed is True
    assert by_key[("grafana", "alerting_rules")].passed is True
    assert by_key[("loki", "labels")].passed is True
    assert by_key[("komodo", "health")].passed is True


def test_verify_monitoring_stack_rejects_empty_prometheus_targets(tmp_path):
    cfg = Config()

    def fake_curl(_cfg, _ip, container, _url, **kwargs):
        if container == "prometheus":
            return 0, '{"data":{"activeTargets":[]}}'
        if container == "grafana":
            if "contact-points" in _url:
                return 0, '[{"uid":"homelab-ntfy"}]'
            if "alert-rules" in _url:
                return 0, '[{"ruleGroup":"homelab-core"}]'
            return 0, '{"status":"OK"}'
        if container == "loki":
            return 0, '{"data":["job"]}'
        if container == "komodo-core":
            return 0, "ok"
        return 255, ""

    with patch("toolkit.core.ansible.ansible_ssh.docker_exec_curl", side_effect=fake_curl):
        with patch("toolkit.core.ops.monitoring_verify._verify_prometheus_reload") as fake_reload:
            with patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "", "")):
                fake_reload.return_value = VerifyCheck("prometheus", "reload", True, "reload ok")
                checks = verify_monitoring_stack(cfg, {"GRAFANA_ADMIN_PASSWORD": "x"}, tmp_path)

    target_check = next(check for check in checks if check.service == "prometheus" and check.check == "targets")
    assert not target_check.passed
    assert "0/0" in target_check.detail
