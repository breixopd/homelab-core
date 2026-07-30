from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.generate.artifacts import ArtifactGenerationContext
from toolkit.services import discover_service_plugins

ROOT = Path(__file__).resolve().parents[3]


def test_alerting_provisioning_is_one_service_owned_read_only_directory() -> None:
    compose = yaml.safe_load((ROOT / "toolkit/services/grafana/compose.yaml").read_text(encoding="utf-8"))
    mounts = compose["services"]["grafana"]["volumes"]
    alerting_mounts = [mount for mount in mounts if "/etc/grafana/provisioning/alerting" in mount]

    assert alerting_mounts == ["${GRAFANA_ALERTING_SOURCE:-./generated/grafana}:/etc/grafana/provisioning/alerting:ro"]


@pytest.mark.parametrize(
    ("email", "notifications", "expected"),
    [
        (False, False, {"homelab-autoheal"}),
        (True, False, {"homelab-autoheal", "homelab-email"}),
        (False, True, {"homelab-autoheal", "homelab-ntfy"}),
    ],
)
def test_grafana_generates_only_enabled_contact_points(
    tmp_path: Path,
    email: bool,
    notifications: bool,
    expected: set[str],
) -> None:
    plugin = next(plugin for plugin in discover_service_plugins() if plugin.service == "grafana")
    cfg = Config(services=ServicesConfig(email=email, notifications=notifications))

    plugin.generate_artifacts(ArtifactGenerationContext(cfg, tmp_path, {}, plugin.manifest))

    rendered = yaml.safe_load((tmp_path / "generated/grafana/contact-points.yaml").read_text(encoding="utf-8"))
    assert {receiver["uid"] for receiver in rendered["contactPoints"][0]["receivers"]} == expected
    assert (tmp_path / "generated/grafana/policies.yaml").is_file()
    assert (tmp_path / "generated/grafana/rules.yaml").is_file()


def test_grafana_autoheal_receiver_uses_internal_hmac_timestamp_contract() -> None:
    document = yaml.safe_load(
        (ROOT / "toolkit/services/grafana/templates/contact-points.yaml").read_text(encoding="utf-8")
    )
    receivers = document["contactPoints"][0]["receivers"]
    receiver = next(item for item in receivers if item["uid"] == "homelab-autoheal")

    assert receiver["settings"] == {
        "url": "http://homelab-ui:8080/api/webhooks/grafana-alert",
        "httpMethod": "POST",
        "maxAlerts": "16",
        "hmacConfig": {
            "secret": "$__env{GRAFANA_WEBHOOK_HMAC_SECRET}",
            "header": "X-Grafana-Alerting-Signature",
            "timestampHeader": "X-Grafana-Alerting-Signature-Timestamp",
        },
    }
    assert receiver["disableResolveMessage"] is True


def test_webhook_secret_is_declared_for_grafana_but_not_homelab_ui() -> None:
    stack = yaml.safe_load((ROOT / "docker-compose.example.yml").read_text(encoding="utf-8"))
    grafana_environment = stack["services"]["grafana"]["environment"]
    ui_environment = stack["services"]["homelab-ui"]["environment"]

    assert grafana_environment["GRAFANA_WEBHOOK_HMAC_SECRET"] == "${GRAFANA_WEBHOOK_HMAC_SECRET}"
    assert "GRAFANA_WEBHOOK_HMAC_SECRET" not in ui_environment


def test_container_missing_rule_uses_error_safe_annotation() -> None:
    document = yaml.safe_load((ROOT / "toolkit/services/grafana/templates/rules.yaml").read_text(encoding="utf-8"))
    rules = document["groups"][0]["rules"]
    rule = next(item for item in rules if item["uid"] == "homelab-managed-container-missing")

    assert rule["labels"] == {"severity": "warning"}
    assert rule["annotations"]["summary"] == "A managed container stopped reporting"
    assert "container_last_seen" in rule["data"][0]["model"]["expr"]
    assert rule["noDataState"] == "OK"
    stack = yaml.safe_load((ROOT / "docker-compose.example.yml").read_text(encoding="utf-8"))
    for service_name in ("cadvisor", "cadvisor-agent"):
        command = stack["services"][service_name]["command"]
        assert "--store_container_labels=false" in command
        assert "--whitelisted_container_labels=com.docker.compose.service" in command
