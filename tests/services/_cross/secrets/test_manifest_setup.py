from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.setup import setup_credentials_from_environment


def test_setup_credentials_from_environment_are_active_typed_and_plugin_prepared() -> None:
    config = Config(
        service_settings={
            "gluetun": {"enabled": True, "provider": "protonvpn"},
            "media-library": {"server": "jellyfin"},
        }
    )
    environment = {
        "HOMELAB_SECRET_VPN_USER": "vpn-user",
        "HOMELAB_SECRET_VPN_PASSWORD": "vpn-password",
        "HOMELAB_SECRET_NORDVPN_TOKEN": "inactive-token",
        "HOMELAB_SECRET_PLEX_CLAIM": "inactive-claim",
    }

    credentials = setup_credentials_from_environment(config, environment)

    assert credentials == {
        "VPN_USER": "vpn-user",
        "VPN_PASSWORD": "vpn-password",
        "VPN_PROVIDER": "protonvpn",
        "VPN_TYPE": "openvpn",
    }
