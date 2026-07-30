"""Toolkit plugin SDK — self-contained primitives for service plugins.

Package layout:

* **Leaf / cfg-free** (``http``, ``docker``, ``registry``): stdlib + httpx only;
  explicit host/IP params, no ``Config``.
* **Cfg-aware** (``authelia``, ``postgres``, ``redis``, ``monitoring``, ``vaultwarden``,
  ``adguard``, ``caddy``, ``wazuh``, ``crowdsec``, ``ldap``): take a ``Config`` and
  centralise URLs/maps previously duplicated across plugins and core.
* **Internal** (``_vmexec``): multi-VM execution via Ansible SSH; re-exported here
  for the single plugin import surface.

Plugins import **only** from ``toolkit.services.sdk``::

    from toolkit.services.sdk import VerifyCheck, docker_exec_on_vm, authelia_oidc_issuer
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password  # noqa: E402
from toolkit.core.verify.models import VerifyCheck  # noqa: E402
from toolkit.services.sdk._vmexec import (
    container_exists_on_vm,
    docker_curl,
    docker_exec_on_vm,
    docker_health_status_on_vm,
    parse_curl_headers,
    ssh_on_vm,
)
from toolkit.services.sdk.adguard import adguard_control_url, adguard_list_rewrites
from toolkit.services.sdk.authelia import (
    authelia_forward_auth_block,
    authelia_oidc_discovery,
    authelia_oidc_issuer,
    authelia_public_url,
    authelia_public_url_for_domain,
    oidc_check_auth_discovery_route,
    oidc_check_env_issuer,
)
from toolkit.services.sdk.caddy import caddy_cross_vm_upstream, caddy_forward_auth_block, caddy_reload_cmd
from toolkit.services.sdk.contracts import validate_integration_contract
from toolkit.services.sdk.crowdsec import crowdsec_cscli, crowdsec_health_url, crowdsec_lapi_url
from toolkit.services.sdk.docker import (
    container_exists,
    docker_exec,
    docker_health_status,
    resolve_service_url,
)
from toolkit.services.sdk.host_agents import systemd_unit_active
from toolkit.services.sdk.http import (
    basic_auth_header,
    http_check,
    http_health_check,
    wait_for_http,
)
from toolkit.services.sdk.ldap import (
    admin_dn,
    base_dn,
    bind_dn,
    ldap_bind_search_on_vm,
    ldap_host,
    ldap_url,
    lldap_bind_uid,
    lldap_group_ou,
    lldap_http_port,
    lldap_http_url,
    lldap_ldap_port,
    lldap_user_ou,
)
from toolkit.services.sdk.monitoring import (
    loki_internal_url,
    loki_url,
    prometheus_internal_url,
    prometheus_reload_url,
    prometheus_url,
)
from toolkit.services.sdk.postgres import (
    PostgresReconcileResult,
    ensure_postgres_healthy,
    reconcile_service_databases,
    sync_project_postgres_databases,
)
from toolkit.services.sdk.redis import redis_port
from toolkit.services.sdk.registry import registry_mirror_ca_url, registry_mirror_port, registry_mirror_running
from toolkit.services.sdk.vaultwarden import (
    BITWARDEN_CLIENT_VERSION,
    vaultwarden_admin_session,
    vaultwarden_fetch_kdf,
    vaultwarden_login_access_token,
    vaultwarden_sync_catalog,
    vaultwarden_url,
)
from toolkit.services.sdk.wazuh import (
    wazuh_agent_control_cmd,
    wazuh_list_agents,
    wazuh_parse_agent_lines,
)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "VerifyCheck",
    "Config",
    "resolve_bootstrap_password",
    "http_check",
    "http_health_check",
    "wait_for_http",
    "systemd_unit_active",
    "basic_auth_header",
    "docker_exec",
    "docker_health_status",
    "container_exists",
    "resolve_service_url",
    "docker_exec_on_vm",
    "ssh_on_vm",
    "docker_curl",
    "docker_health_status_on_vm",
    "container_exists_on_vm",
    "parse_curl_headers",
    "authelia_public_url",
    "authelia_public_url_for_domain",
    "authelia_oidc_issuer",
    "authelia_forward_auth_block",
    "oidc_check_env_issuer",
    "oidc_check_auth_discovery_route",
    "authelia_oidc_discovery",
    "redis_port",
    "prometheus_url",
    "prometheus_internal_url",
    "prometheus_reload_url",
    "loki_url",
    "loki_internal_url",
    "PostgresReconcileResult",
    "ensure_postgres_healthy",
    "reconcile_service_databases",
    "sync_project_postgres_databases",
    "BITWARDEN_CLIENT_VERSION",
    "vaultwarden_url",
    "vaultwarden_admin_session",
    "vaultwarden_sync_catalog",
    "vaultwarden_fetch_kdf",
    "vaultwarden_login_access_token",
    "adguard_control_url",
    "adguard_list_rewrites",
    "caddy_forward_auth_block",
    "caddy_cross_vm_upstream",
    "caddy_reload_cmd",
    "wazuh_agent_control_cmd",
    "wazuh_list_agents",
    "wazuh_parse_agent_lines",
    "crowdsec_lapi_url",
    "crowdsec_health_url",
    "crowdsec_cscli",
    "validate_integration_contract",
    "admin_dn",
    "base_dn",
    "bind_dn",
    "ldap_host",
    "ldap_bind_search_on_vm",
    "ldap_url",
    "lldap_http_url",
    "lldap_bind_uid",
    "lldap_group_ou",
    "lldap_http_port",
    "lldap_ldap_port",
    "lldap_user_ou",
    "registry_mirror_port",
    "registry_mirror_ca_url",
    "registry_mirror_running",
]
