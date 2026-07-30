from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG, ansible_roles_for_services
from toolkit.core.manifest.schema import HostIntegrationFieldManifest, HostIntegrationManifest


def test_host_service_catalog_is_derived_from_service_manifests() -> None:
    integrations = {service.name: service for service in FLEET_SERVICE_CATALOG}

    assert set(integrations) == {
        "monitoring-agent",
        "wazuh-agent",
        "crowdsec-agent",
        "komodo-periphery",
        "vpn-client",
        "dns-client",
        "ldap-client",
        "media-cache",
        "backup-storage",
    }
    assert integrations["monitoring-agent"].owner == "node-exporter"
    assert integrations["komodo-periphery"].owner == "komodo-core"
    assert integrations["komodo-periphery"].kinds == ("fleet",)
    assert integrations["media-cache"].owner == "media-cache"
    assert integrations["media-cache"].ansible_role == ""
    assert integrations["ldap-client"].kinds == ("fleet",)
    assert integrations["media-cache"].controller_lifecycle is True
    assert integrations["media-cache"].fields[0].key == "path"
    assert integrations["media-cache"].fields[0].type == "path"
    assert integrations["media-cache"].fields[0].required is True
    assert integrations["backup-storage"].fields[0].key == "path"
    assert integrations["crowdsec-agent"].after == ("vpn-client",)
    assert list(integrations).index("vpn-client") < list(integrations).index("crowdsec-agent")


def test_selected_host_services_compile_to_unique_ansible_roles() -> None:
    assert ansible_roles_for_services(
        ["monitoring-agent", "media-cache", "monitoring-agent", "backup-storage"],
        kind="plain",
    ) == ["monitoring_agent", "backup_storage"]
    assert ansible_roles_for_services(
        ["crowdsec-agent", "vpn-client"],
        kind="fleet",
    ) == ["vpn_client", "crowdsec_agent"]


def test_every_manifest_declared_host_agent_has_an_ansible_role() -> None:
    roles_root = Path(__file__).resolve().parents[4] / "automation" / "ansible" / "roles"

    for service in FLEET_SERVICE_CATALOG:
        if service.ansible_role:
            assert (roles_root / service.ansible_role / "tasks" / "main.yml").is_file(), service.name


def test_host_integration_field_defaults_reject_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="finite"):
        HostIntegrationFieldManifest(
            key="ratio",
            label="Ratio",
            type="number",
            default=float("nan"),
        )


def test_host_integration_ordering_rejects_self_dependency() -> None:
    with pytest.raises(ValueError, match="cannot reference itself"):
        HostIntegrationManifest(
            id="agent",
            label="Agent",
            kinds=("fleet",),
            after=("agent",),
        )
