"""VPN provider configuration for gluetun.

Builds the gluetun ``.env.vpn`` from stored secrets for any supported provider,
preferring WireGuard where possible. For NordVPN, the WireGuard (NordLynx) private
key is derived automatically from a NordVPN access token so the user only ever has
to supply the token once.
"""

from __future__ import annotations

NORDVPN_CRED_API = "https://api.nordvpn.com/v1/users/services/credentials"

# Preferred gluetun VPN_TYPE per provider when the user does not specify one.
# WireGuard is preferred for reliability/speed where the provider supports it in gluetun.
DEFAULT_VPN_TYPE: dict[str, str] = {
    "nordvpn": "wireguard",
    "protonvpn": "wireguard",
    "mullvad": "wireguard",
    "surfshark": "wireguard",
    "windscribe": "wireguard",
    "privado": "openvpn",
    "pia": "wireguard",
    "private internet access": "wireguard",
    "airvpn": "wireguard",
    "ivpn": "wireguard",
    "cyberghost": "openvpn",
    "expressvpn": "openvpn",
    "custom": "wireguard",
}

# Providers known to gluetun (used for light validation / UI dropdowns).
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "nordvpn",
    "protonvpn",
    "mullvad",
    "surfshark",
    "windscribe",
    "pia",
    "airvpn",
    "ivpn",
    "cyberghost",
    "expressvpn",
    "custom",
)


def fetch_nordvpn_wireguard_key(token: str, *, timeout: int = 25) -> str:
    """Return the NordLynx (WireGuard) private key for a NordVPN access token.

    The token is generated at NordVPN -> Manual setup -> "Generate new token".
    """
    import httpx

    token = token.strip()
    if not token:
        raise ValueError("NordVPN access token is empty")
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(NORDVPN_CRED_API, auth=("token", token))
    resp.raise_for_status()
    data = resp.json()
    key = (data or {}).get("nordlynx_private_key", "")
    if not key:
        raise ValueError("NordVPN API returned no nordlynx_private_key (token invalid or expired)")
    return key


def resolve_vpn_type(provider: str, vpn_type: str = "") -> str:
    provider = (provider or "").strip().lower()
    vpn_type = (vpn_type or "").strip().lower()
    if vpn_type:
        return vpn_type
    return DEFAULT_VPN_TYPE.get(provider, "openvpn")


_VPN_SECRET_NAMES = frozenset(
    {
        "VPN_PROVIDER",
        "VPN_TYPE",
        "NORDVPN_TOKEN",
        "VPN_USER",
        "VPN_PASSWORD",
        "VPN_SERVER_COUNTRIES",
        "WIREGUARD_PRIVATE_KEY",
        "WIREGUARD_ADDRESSES",
    }
)


def visible_vpn_secret_names(secrets: dict[str, str]) -> set[str]:
    """Return VPN secret keys to show in CLI/UI for the configured provider."""
    provider = (secrets.get("VPN_PROVIDER") or "").strip().lower()
    if not provider:
        return {"VPN_PROVIDER", "NORDVPN_TOKEN", "VPN_SERVER_COUNTRIES"}
    vpn_type = resolve_vpn_type(provider, secrets.get("VPN_TYPE", ""))
    names = {"VPN_PROVIDER", "VPN_SERVER_COUNTRIES"}
    if provider == "nordvpn":
        names.add("NORDVPN_TOKEN")
        return names
    names.add("VPN_TYPE")
    if provider == "custom" and vpn_type == "wireguard":
        names.update({"WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"})
    else:
        names.update({"VPN_USER", "VPN_PASSWORD"})
    return names


def filter_vpn_specs(specs, secrets: dict[str, str]):
    """Drop VPN secret specs that do not apply to the selected provider."""
    visible = visible_vpn_secret_names(secrets)
    return [s for s in specs if s.name not in _VPN_SECRET_NAMES or s.name in visible]


def apply_nordvpn_secrets(secrets: dict[str, str], token: str) -> dict[str, str]:
    """Set NordVPN WireGuard credentials (token-only; key is derived at generate time)."""
    updated = dict(secrets)
    updated["VPN_PROVIDER"] = "nordvpn"
    updated["VPN_TYPE"] = "wireguard"
    updated["NORDVPN_TOKEN"] = token.strip()
    for obsolete in ("VPN_USER", "VPN_PASSWORD", "WIREGUARD_PRIVATE_KEY"):
        updated.pop(obsolete, None)
    return updated


def build_vpn_env(secrets: dict[str, str]) -> tuple[dict[str, str], str]:
    """Build gluetun .env.vpn variables from secrets.

    Returns ``(env, derived_wireguard_key)``. ``derived_wireguard_key`` is non-empty
    only when it was freshly derived (e.g. from a NordVPN token) and should be cached
    back into secrets by the caller.
    """
    provider = (secrets.get("VPN_PROVIDER") or "").strip().lower()
    vpn_type = resolve_vpn_type(provider, secrets.get("VPN_TYPE", ""))

    wg_key = (secrets.get("WIREGUARD_PRIVATE_KEY") or "").strip()
    wg_addr = (secrets.get("WIREGUARD_ADDRESSES") or "").strip()
    derived = ""

    # NordVPN WireGuard: derive the private key from the access token when missing.
    if provider == "nordvpn" and vpn_type == "wireguard" and not wg_key:
        token = (secrets.get("NORDVPN_TOKEN") or "").strip()
        if token:
            wg_key = fetch_nordvpn_wireguard_key(token)
            derived = wg_key

    env = {
        "VPN_SERVICE_PROVIDER": provider,
        "VPN_TYPE": vpn_type,
        "OPENVPN_USER": secrets.get("VPN_USER", ""),
        "OPENVPN_PASSWORD": secrets.get("VPN_PASSWORD", ""),
        "SERVER_COUNTRIES": secrets.get("VPN_SERVER_COUNTRIES", ""),
        "WIREGUARD_PRIVATE_KEY": wg_key,
        # gluetun auto-fills addresses for known providers; required only for custom.
        "WIREGUARD_ADDRESSES": wg_addr,
        # Allow declared media integrations to reach qBittorrent's
        # WebUI (port 8080) which shares gluetun's network namespace via
        # network_mode: service:gluetun. Without this, gluetun's default
        # firewall (policy DROP on INPUT) blocks Sonarr/Radarr from connecting.
        "FIREWALL_INPUT_PORTS": "8080",
        # Allow the local Docker subnet through gluetun's firewall so
        # containers on compiler-managed plugin links can reach qBittorrent.
        "FIREWALL_OUTBOUND_SUBNETS": "172.16.0.0/12",
    }
    return env, derived
