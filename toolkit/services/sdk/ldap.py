"""LDAP helpers — re-exported from :mod:`toolkit.core.config.ldap`.

Plugins import LDAP resolution from here rather than reaching into ``toolkit.core.*``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.config.ldap import (  # noqa: F401
    admin_dn,
    base_dn,
    bind_dn,
    ldap_host,
    ldap_url,
    lldap_bind_uid,
    lldap_group_ou,
    lldap_http_port,
    lldap_http_url,
    lldap_ldap_port,
    lldap_user_ou,
)
from toolkit.services.sdk._vmexec import docker_exec_on_vm

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


_LDAP_BIND_SEARCH = """set -eu
umask 077
password_file=$(mktemp)
trap 'rm -f "$password_file"' EXIT HUP INT TERM
printf '%s' "$LLDAP_BIND_PASSWORD" > "$password_file"
ldapsearch -x -H "$1" -D "$2" -y "$password_file" -b "$3" "$4" dn
"""


def ldap_bind_search_on_vm(
    cfg: Config,
    vm_ip: str,
    root: Path,
    *,
    bind_password: str,
    bind_dn_value: str,
    base_dn_value: str,
    search_filter: str,
) -> tuple[int, str]:
    """Verify an LDAP bind using the LLDAP service-owned client utility.

    Credentials travel through the hardened stdin-backed secret environment.
    ``ldapsearch`` reads them from a transient owner-only file, so neither the
    password nor LDAP query appear in a process argument.
    """
    return docker_exec_on_vm(
        cfg,
        "lldap",
        [
            "sh",
            "-ec",
            _LDAP_BIND_SEARCH,
            "lldap-ldap-bind-probe",
            f"ldap://127.0.0.1:{lldap_ldap_port()}",
            bind_dn_value,
            base_dn_value,
            search_filter,
        ],
        vm_ip,
        root,
        timeout=30,
        secret_environment={"LLDAP_BIND_PASSWORD": bind_password},
    )


__all__ = [
    "admin_dn",
    "base_dn",
    "bind_dn",
    "ldap_host",
    "ldap_url",
    "lldap_http_url",
    "ldap_bind_search_on_vm",
    "lldap_bind_uid",
    "lldap_group_ou",
    "lldap_http_port",
    "lldap_ldap_port",
    "lldap_user_ou",
]
