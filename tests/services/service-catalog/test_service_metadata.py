from __future__ import annotations

from toolkit.core.config import service_metadata


def test_service_metadata_loads_the_manifest_catalog():
    service_metadata._load_all_services.cache_clear()

    metadata = service_metadata._load_all_services()

    assert "immich-machine-learning" in metadata
    assert metadata["immich-machine-learning"]["category"] == "cloud"
    assert metadata["immich-machine-learning"]["memory_tier"] == "heavy"
    assert all("_source" not in entry for entry in metadata.values())


def test_service_metadata_exposes_phase_n_services_to_consumers():
    service_metadata._load_all_services.cache_clear()
    service_metadata._runtime_service_owners.cache_clear()

    assert service_metadata.get_service_restart_policy("uptime-kuma") == "careful"
    assert service_metadata.get_service_memory_tier("crowdsec") == "medium"
    assert "immich-server" in service_metadata.get_service_depends_on("immich-machine-learning")
    assert service_metadata.get_service_resource_requirements("romm") == (1024, 0.5)
    assert service_metadata.get_service_resource_requirements("kopia") == (2048, 0.1)
    assert service_metadata.get_service_resource_requirements("kopia-agent") == (1024, 0.1)


def test_essential_safe_services_are_not_reported_as_safe_to_restart():
    service_metadata._load_all_services.cache_clear()

    assert "postgres" in service_metadata.never_restart_services()
    assert "postgres" not in service_metadata.safe_to_restart_services()
