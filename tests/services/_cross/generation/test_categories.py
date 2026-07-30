from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from toolkit.categories import Category
from toolkit.categories.schema import CategoryManifest
from toolkit.core.compose.registry import all_categories, dependency_sort, enabled_categories
from toolkit.core.config.config import Config


def test_all_categories_registered():
    cats = all_categories()
    names = [c.name for c in cats]
    assert len(names) == 6
    for expected in [
        "management",
        "media",
        "cloud",
        "notifications",
        "email",
        "security",
    ]:
        assert expected in names, f"{expected} not registered"


def test_management_always_on():
    cats = all_categories()
    mgmt = next(c for c in cats if c.name == "management")
    assert mgmt.always_on is True


def test_enabled_categories_default_config():
    config = Config()
    cats = enabled_categories(config)
    names = [c.name for c in cats]
    assert len(names) == 6


def test_enabled_categories_partial():
    config = Config(
        services={
            "management": True,
            "media": True,
            "cloud": False,
            "notifications": False,
            "email": False,
            "security": False,
        }
    )
    cats = enabled_categories(config)
    names = [c.name for c in cats]
    assert "management" in names
    assert "media" in names
    assert "cloud" not in names


def test_dependency_sort_management_first():
    config = Config()
    cats = enabled_categories(config)
    sorted_cats = dependency_sort(cats)
    assert sorted_cats[0].name == "management"


def test_category_manifests_are_strict_and_plugin_defined() -> None:
    manifest = CategoryManifest.model_validate({"name": "photos", "label": "Photos", "compose_profiles": ["photos"]})

    assert manifest.name == "photos"
    with pytest.raises(ValidationError):
        CategoryManifest.model_validate({"name": "photos", "label": "Photos", "unknown": True})
    with pytest.raises(ValidationError):
        CategoryManifest.model_validate(
            {"name": "photos", "label": "Photos", "depends_on": ["management", "management"]}
        )


def test_access_groups_are_category_plugin_owned() -> None:
    groups = {
        category.access_group.name: category.access_group for category in all_categories() if category.access_group
    }

    assert set(groups) == {"homelab-admin", "homelab-media", "homelab-cloud"}
    assert groups["homelab-media"].default_invite is True
    assert groups["homelab-cloud"].default_invite is True
    assert groups["homelab-admin"].default_invite is False
    assert groups["homelab-admin"].administrator is True
    assert all(category.service_group for category in all_categories())


def test_category_manifest_validates_access_group_contract() -> None:
    manifest = CategoryManifest.model_validate(
        {
            "name": "photos",
            "label": "Photos",
            "service_group": "homelab-family",
            "access_group": {
                "name": "homelab-family",
                "label": "Family",
                "description": "Family applications",
                "default_invite": True,
            },
        }
    )

    assert manifest.access_group is not None
    assert manifest.access_group.name == manifest.service_group


def test_category_dependency_sort_fails_closed() -> None:
    first = Category(name="first", label="First", compose_file="docker-compose.yml", _depends_on=["second"])
    second = Category(name="second", label="Second", compose_file="docker-compose.yml", _depends_on=["first"])

    with pytest.raises(ValueError, match="cycle"):
        dependency_sort([first, second])
    with pytest.raises(ValueError, match="unavailable"):
        dependency_sort([first])


def test_category_loader_rejects_unknown_callbacks(monkeypatch) -> None:
    from toolkit.categories.yaml_loader import load_category_from_yaml

    manifest = CategoryManifest.model_validate({"name": "photos", "label": "Photos", "validate": "missing_callback"})
    monkeypatch.setattr("toolkit.categories.yaml_loader.load_category_yaml", lambda _name: manifest)

    with pytest.raises(ValueError, match="unknown validation callback"):
        load_category_from_yaml("photos")


def test_media_services_include_required_orchestration_only():
    config = Config()
    cats = all_categories()
    media = next(c for c in cats if c.name == "media")
    svcs = media.services(config)
    names = {service.name for service in svcs}

    assert {"media-library", "servarr", "jellyfin"} <= names
    assert {"plex", "tautulli"}.isdisjoint(names)


def test_media_without_vpn():
    config = Config(service_settings={"gluetun": {"enabled": False}})
    cats = all_categories()
    media = next(c for c in cats if c.name == "media")
    svcs = media.services(config)
    names = [s.name for s in svcs]
    assert "gluetun" not in names


def test_media_selected_profiles_default():
    config = Config(
        service_settings={
            "media-library": {"server": "both"},
            "jellyfin": {"hardware-transcode": "none"},
            "gluetun": {"enabled": True},
        }
    )
    media = next(c for c in all_categories() if c.name == "media")

    profiles = media.selected_compose_profiles(config)

    assert "media" in profiles
    assert "media-vpn" in profiles
    assert "media-jellyfin" in profiles
    assert "media-plex" in profiles


def test_media_selected_profiles_vaapi_jellyfin_only(monkeypatch):
    from toolkit.core.capabilities import GpuCapabilities

    monkeypatch.setattr(
        "toolkit.core.capabilities.detect_gpu_for_vm",
        lambda *a, **k: GpuCapabilities(backend="vaapi", source="lxc:media"),
    )
    config = Config(
        service_settings={
            "media-library": {"server": "jellyfin"},
            "jellyfin": {"hardware-transcode": "vaapi"},
            "gluetun": {"enabled": False},
        }
    )
    media = next(c for c in all_categories() if c.name == "media")

    profiles = media.selected_compose_profiles(config)

    assert "media" in profiles
    assert "media-vpn" not in profiles
    assert "media-jellyfin-vaapi" in profiles
    assert "media-jellyfin" not in profiles
    assert "media-plex" not in profiles


def test_media_selected_profiles_downgrades_vaapi_without_gpu(monkeypatch):
    from toolkit.core.capabilities import GpuCapabilities

    monkeypatch.setattr(
        "toolkit.core.capabilities.detect_gpu_for_vm",
        lambda *a, **k: GpuCapabilities(backend="none", source="lxc:media"),
    )
    config = Config(
        service_settings={
            "media-library": {"server": "jellyfin"},
            "jellyfin": {"hardware-transcode": "vaapi"},
            "gluetun": {"enabled": False},
        }
    )
    media = next(c for c in all_categories() if c.name == "media")

    profiles = media.selected_compose_profiles(config)

    assert "media-jellyfin" in profiles
    assert "media-jellyfin-vaapi" not in profiles


def test_management_services_count():
    config = Config()
    mgmt = next(c for c in all_categories() if c.name == "management")
    assert len(mgmt.services(config)) == 19  # Kopia is enabled only when backups are configured.


def test_media_local_path_consults_capability_cache(monkeypatch):
    """F1 unification: the local GPU-detection path must route through the
    cached load_capabilities() rather than re-probing /dev and nvidia-smi
    on every generate cycle. The multi-VM SSH path stays on detect_gpu_for_vm
    (a fresh per-deploy probe is wanted there, not a stale cache hit)."""
    from toolkit.core.capabilities import GpuCapabilities, ServerCapabilities
    from toolkit.core.infra.host_capacity import HostCapacity

    cached = ServerCapabilities(
        host=HostCapacity(
            cpu_cores=4,
            mem_total_mb=8192,
            load_1m=0.5,
            wave_timeout_s=180,
            inter_wave_sleep_s=5,
            max_pull_parallel=2,
            load_threshold=8.0,
            source="local",
        ),
        gpu=GpuCapabilities(backend="vaapi", source="cache"),
        vm="local",
        has_aes_ni=True,
        disk_type="ssd",
        cpu_model="Test",
        detected_at="2026-06-25T00:00:00+00:00",
    )

    load_calls = []

    def tracking_load(vm="local", *, root=".", force_refresh=False):
        load_calls.append(vm)
        return cached

    # hooks.py does `from toolkit.core.capabilities import ... load_capabilities`
    # at call time, binding to the package __init__'s attribute. Patch that
    # attribute (not store.load_capabilities) so the tracker is actually used.
    import toolkit.core.capabilities as caps_pkg

    monkeypatch.setattr(caps_pkg, "load_capabilities", tracking_load)
    # Force the ON-MEDIA-GUEST local path: HOMELAB_NODE=media means we're
    # generating configs ON the media LXC, where the cached capability snapshot
    # is the right call (no SSH needed; we're already on the box). This is the
    # high-frequency local path during real deploys — the multi-VM SSH path
    # (detect_gpu_for_vm) is the cold/rare one.
    monkeypatch.setenv("HOMELAB_NODE", "media")
    config = Config(
        service_settings={
            "media-library": {"server": "jellyfin"},
            "jellyfin": {"hardware-transcode": "vaapi"},
            "gluetun": {"enabled": False},
        }
    )
    media = next(c for c in all_categories() if c.name == "media")

    profiles = media.selected_compose_profiles(config)

    assert load_calls, "load_capabilities was never consulted on the local path"
    assert "media-jellyfin-vaapi" in profiles  # cache returned vaapi → vaapi profile selected


def test_all_categories_have_compose_file():
    for cat in all_categories():
        assert cat.compose_file == "docker-compose.yml", f"{cat.name} compose_file invalid"


def test_cloud_services_include_dev_tools():
    config = Config()
    cloud = next(c for c in all_categories() if c.name == "cloud")
    names = [s.name for s in cloud.services(config)]
    assert "gitea" in names
    assert "dev-postgres" in names
    assert "dev-redis" in names


def test_category_service_membership_has_no_parallel_registry() -> None:
    categories_root = Path(__file__).resolve().parents[4] / "toolkit" / "categories"
    for path in categories_root.glob("*/category.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "services" not in document, path
        assert "services_override" not in document, path
        assert "variables" not in document, path
        assert "required_secrets" not in document, path
        assert "data_specs" not in document, path
        assert "host_services" not in document, path


def test_category_validate():
    cats = all_categories()
    media = next(c for c in cats if c.name == "media")
    cfg = Config(domain="test.local")
    errors = media.validate(cfg)
    assert isinstance(errors, list)
