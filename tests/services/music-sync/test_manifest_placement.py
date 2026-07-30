"""Music Sync runtime placement is owned by the service suite."""

from __future__ import annotations

from pathlib import Path

import yaml
from tests.helpers.machines import single_control_machines
from toolkit.core.config.config import Config
from toolkit.services import get_service_plugin

ROOT = Path(__file__).resolve().parents[3]


def test_service_plugin_resolves_its_manifest_owned_runtime() -> None:
    cfg = Config(domain="example.com")
    plugin = get_service_plugin("music-sync")

    assert plugin is not None
    assert plugin.runtime_node(cfg) == "media"
    assert plugin.runtime_address(cfg) == "10.10.10.11"
    assert plugin.runtime_address(Config(machines=single_control_machines())) == "localhost"


def test_read_only_runtime_declares_transient_home_cache_and_persistent_auth_paths() -> None:
    compose = yaml.safe_load((ROOT / "toolkit/services/music-sync/compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["music-sync"]
    environment = service["environment"]

    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]
    assert environment["HOME"] == "/tmp"
    assert environment["XDG_CACHE_HOME"] == "/tmp/cache"
    assert environment["SPOTIFY_CACHE_PATH"] == "/config/spotify/spotipy-token.json"
    assert environment["YTMUSIC_AUTH_FILE"] == "/config/ytmusic/headers_auth.json"
