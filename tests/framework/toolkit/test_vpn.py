from toolkit.core.config.config import Config
from toolkit.core.ops.vpn import (
    DEFAULT_VPN_TYPE,
    apply_nordvpn_secrets,
    build_vpn_env,
    filter_vpn_specs,
    resolve_vpn_type,
    visible_vpn_secret_names,
)
from toolkit.core.secrets.secrets import get_required_secrets


def test_resolve_vpn_type_defaults():
    assert resolve_vpn_type("nordvpn") == "wireguard"
    assert resolve_vpn_type("cyberghost") == "openvpn"
    assert resolve_vpn_type("unknown-provider") == "openvpn"


def test_resolve_vpn_type_explicit_wins():
    assert resolve_vpn_type("nordvpn", "openvpn") == "openvpn"


def test_build_vpn_env_openvpn_provider():
    env, derived = build_vpn_env(
        {
            "VPN_PROVIDER": "cyberghost",
            "VPN_USER": "u",
            "VPN_PASSWORD": "p",
        }
    )
    assert env["VPN_SERVICE_PROVIDER"] == "cyberghost"
    assert env["VPN_TYPE"] == "openvpn"
    assert env["OPENVPN_USER"] == "u"
    assert env["OPENVPN_PASSWORD"] == "p"
    assert derived == ""


def test_build_vpn_env_custom_wireguard_uses_stored_key():
    env, derived = build_vpn_env(
        {
            "VPN_PROVIDER": "custom",
            "VPN_TYPE": "wireguard",
            "WIREGUARD_PRIVATE_KEY": "abc123",
            "WIREGUARD_ADDRESSES": "10.2.0.2/32",
        }
    )
    assert env["VPN_TYPE"] == "wireguard"
    assert env["WIREGUARD_PRIVATE_KEY"] == "abc123"
    assert env["WIREGUARD_ADDRESSES"] == "10.2.0.2/32"
    assert derived == ""


def test_nordvpn_in_default_type_map():
    assert DEFAULT_VPN_TYPE["nordvpn"] == "wireguard"


def test_visible_vpn_secret_names_nordvpn():
    names = visible_vpn_secret_names({"VPN_PROVIDER": "nordvpn"})
    assert names == {"VPN_PROVIDER", "NORDVPN_TOKEN", "VPN_SERVER_COUNTRIES"}


def test_filter_vpn_specs_hides_openvpn_creds_for_nordvpn():
    specs = filter_vpn_specs(get_required_secrets(Config()), {"VPN_PROVIDER": "nordvpn"})
    names = {s.name for s in specs}
    assert "NORDVPN_TOKEN" in names
    assert "VPN_USER" not in names
    assert "VPN_PASSWORD" not in names


def test_apply_nordvpn_secrets_clears_incompatible_provider_fields():
    updated = apply_nordvpn_secrets(
        {"VPN_USER": "old", "VPN_PASSWORD": "old", "WIREGUARD_PRIVATE_KEY": "k"},
        "token123",
    )
    assert updated["VPN_PROVIDER"] == "nordvpn"
    assert updated["NORDVPN_TOKEN"] == "token123"
    assert "VPN_USER" not in updated
    assert "VPN_PASSWORD" not in updated
    assert "WIREGUARD_PRIVATE_KEY" not in updated
