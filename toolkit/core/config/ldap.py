"""Centralised LDAP/LDAP-host resolution for the homelab.

Every service that talks to LLDAP (Authelia, Docker-Mailserver, Jellyfin's
LDAP plugin, SSSD on fleet nodes, Roundcube, etc.) used to compute
the connection details inline — sometimes differently, sometimes wrong. This
module is the single source of truth.

Three host modes are supported via one ``ready`` ``Config``:
- multi-VM: ``ldap://<service_ip>:<published-port>`` (cross-VM clients use
  the LLDAP manifest's published endpoint).
- single-VM: ``ldap://lldap:<container-port>`` (Docker DNS, intra-Compose).
- fleet hosts: ``LLDAP_LDAP_URL`` may override the generated endpoint when a
  mesh or site-specific DNS name is required.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _lldap_manifest_value(name: str) -> str:
    """Read a static LLDAP contract value from the service manifest."""
    from toolkit.core.manifest.catalog import load_service_catalog

    value = load_service_catalog().require("lldap").variables.get(name)
    if not isinstance(value, str) or not value or "{" in value or "}" in value:
        raise ValueError(f"LLDAP manifest variable {name!r} must be a static service contract value")
    return value


def lldap_bind_uid() -> str:
    """Return the service-owned LDAP bind account UID."""
    return _lldap_manifest_value("LLDAP_BIND_UID")


def lldap_user_ou() -> str:
    """Return the service-owned user OU."""
    return _lldap_manifest_value("LLDAP_USER_OU")


def lldap_group_ou() -> str:
    """Return the service-owned group OU."""
    return _lldap_manifest_value("LLDAP_GROUP_OU")


def lldap_ldap_port(*, published: bool = False) -> int:
    """Return LLDAP's manifest-owned container or published LDAP port."""
    from toolkit.core.manifest.placement import service_endpoint_port

    return service_endpoint_port("lldap", published=published)


def lldap_http_port() -> int:
    """Return LLDAP's manifest-owned HTTP route port."""
    from toolkit.core.manifest.placement import service_route_port

    return service_route_port("lldap", subdomain="users")


def base_dn(cfg: Config) -> str:
    """Return the LDAP base DN derived from ``cfg.domain`` (e.g. ``dc=example,dc=org``).

    The special ``localhost`` dev domain uses ``dc=home,dc=local`` as the
    dev default, matching the manifest variable LDAP base-DN derivation.
    """
    domain = (cfg.domain or "home.local").strip(".")
    if not domain or domain == "localhost":
        return "dc=home,dc=local"
    parts = [f"dc={p}" for p in domain.split(".") if p]
    return ",".join(parts) or "dc=home,dc=local"


def bind_dn(cfg: Config) -> str:
    """Return the LLDAP service-account bind DN."""
    return f"cn={lldap_bind_uid()},{lldap_user_ou()},{base_dn(cfg)}"


def ldap_url(cfg: Config) -> str:
    """Return the LDAP URL for cross-VM / external clients.

    Multi-VM uses the LLDAP service's published manifest port. Single-VM uses
    its Compose endpoint. Fleet external hosts use ``LLDAP_LDAP_URL`` when set
    (so they can override to a mesh or site-specific hostname).
    """
    env_override = os.environ.get("LLDAP_LDAP_URL")
    if env_override:
        return env_override
    if cfg.is_multi_node:
        from toolkit.core.manifest.placement import service_address

        return f"ldap://{service_address(cfg, 'lldap')}:{lldap_ldap_port(published=True)}"
    return f"ldap://lldap:{lldap_ldap_port()}"


def ldap_host(cfg: Config) -> str:
    """Return the LLDAP host IP/hostname (no scheme/port).

    Useful for hooks that take the host separately from the port (Jellyfin
    LDAP plugin XML, sssd.conf ``ldap_uri``, etc.).
    """
    if cfg.is_multi_node:
        from toolkit.core.manifest.placement import service_address

        return service_address(cfg, "lldap")
    return "lldap"


def lldap_http_url(cfg: Config) -> str:
    """Return the public-facing LLDAP web UI URL (e.g. https://users.<domain>)."""
    sub = os.environ.get("LLDAP_SUBDOMAIN") or "users"
    return f"https://{sub}.{cfg.domain}"


def admin_dn(cfg: Config) -> str:
    """Return the LLDAP admin DN.

    Only used internally by ``lldap_client`` for management operations.
    """
    return f"cn=admin,{lldap_user_ou()},{base_dn(cfg)}"
