from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from toolkit.core.config.config import Config
from toolkit.services.sdk.ldap import ldap_bind_search_on_vm


def test_ldap_bind_probe_uses_the_lldap_container_and_secret_environment(monkeypatch) -> None:
    execute = MagicMock(return_value=(0, "dn: cn=admin"))
    monkeypatch.setattr("toolkit.services.sdk.ldap.docker_exec_on_vm", execute)

    result = ldap_bind_search_on_vm(
        Config(domain="example.com"),
        "10.10.10.10",
        Path("/tmp"),
        bind_password="test-only-bind-password",
        bind_dn_value="cn=ldap-bind,ou=people,dc=example,dc=com",
        base_dn_value="dc=example,dc=com",
        search_filter="(uid=ldap-bind)",
    )

    assert result == (0, "dn: cn=admin")
    args = execute.call_args.args
    kwargs = execute.call_args.kwargs
    assert args[1] == "lldap"
    assert "test-only-bind-password" not in repr(args[2])
    assert "-w" not in args[2]
    assert kwargs["secret_environment"] == {"LLDAP_BIND_PASSWORD": "test-only-bind-password"}
