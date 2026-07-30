from __future__ import annotations

import pytest
from toolkit.core.config.config import Config
from toolkit.core.manifest.settings import (
    ServiceSettingError,
    service_setting_bool,
    service_setting_int,
    service_setting_str,
)


def test_typed_service_setting_accessors_resolve_defaults_and_overrides() -> None:
    defaults = Config()
    overrides = Config(
        service_settings={
            "gluetun": {"enabled": False},
            "media-library": {"server": "both"},
            "qbittorrent": {"listen-port": 4545},
        }
    )

    assert service_setting_bool(defaults, "gluetun", "enabled") is True
    assert service_setting_bool(overrides, "gluetun", "enabled") is False
    assert service_setting_str(overrides, "media-library", "server") == "both"
    assert service_setting_int(overrides, "qbittorrent", "listen-port") == 4545


def test_typed_service_setting_accessors_reject_wrong_types() -> None:
    cfg = Config()

    with pytest.raises(ServiceSettingError, match="requires a boolean"):
        service_setting_bool(cfg, "media-library", "server")
    with pytest.raises(ServiceSettingError, match="requires text"):
        service_setting_str(cfg, "qbittorrent", "listen-port")
    with pytest.raises(ServiceSettingError, match="requires an integer"):
        service_setting_int(cfg, "gluetun", "enabled")
