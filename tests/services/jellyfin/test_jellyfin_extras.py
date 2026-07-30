from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config
from toolkit.services.jellyfin.extras import (
    LDAP_CONFIG_PATH,
    _configure_jellyfin_ldap,
    _restart_jellyfin,
    configure_jellyfin_extras,
)


def test_restart_polls_host_reachable_base_url() -> None:
    with (
        patch("toolkit.services.jellyfin.extras.subprocess.run") as restart,
        patch("toolkit.services.jellyfin.extras.httpx.get", return_value=MagicMock(status_code=200)) as get,
    ):
        assert _restart_jellyfin("http://172.31.250.140:8096") is True

    restart.assert_called_once()
    get.assert_called_once_with("http://172.31.250.140:8096/health", timeout=3)


def test_extras_are_idempotent_when_plugins_are_installed() -> None:
    installed = {"Webhook", "Intro Skipper", "Open Subtitles", "TMDb Box Sets", "Merge Versions"}
    with (
        patch("toolkit.services.jellyfin.extras._merge_repositories", return_value=(True, False)),
        patch("toolkit.services.jellyfin.extras._installed_plugin_names", return_value=installed),
        patch("toolkit.services.jellyfin.extras._install_package") as install,
        patch("toolkit.services.jellyfin.extras._restart_jellyfin") as restart,
        patch("toolkit.services.jellyfin.extras._configure_webhook", return_value=True),
    ):
        logs = configure_jellyfin_extras(Config(domain="example.com"), "api-key", base_url="http://bridge:8096")

    install.assert_not_called()
    restart.assert_not_called()
    assert any("already installed" in line for line in logs)


def test_extras_restart_once_after_installing_missing_plugins() -> None:
    with (
        patch("toolkit.services.jellyfin.extras._merge_repositories", return_value=(True, False)),
        patch("toolkit.services.jellyfin.extras._installed_plugin_names", return_value=set()),
        patch("toolkit.services.jellyfin.extras._install_package", return_value=True),
        patch("toolkit.services.jellyfin.extras._restart_jellyfin", return_value=True) as restart,
        patch("toolkit.services.jellyfin.extras._configure_webhook", return_value=True),
    ):
        configure_jellyfin_extras(Config(domain="example.com"), "api-key", base_url="http://bridge:8096")

    restart.assert_called_once_with("http://bridge:8096")


def test_ldap_package_alias_is_treated_as_installed() -> None:
    installed = {"Intro Skipper", "Open Subtitles", "TMDb Box Sets", "Merge Versions", "LDAP-Auth"}
    with (
        patch("toolkit.services.jellyfin.extras._merge_repositories", return_value=(True, False)),
        patch("toolkit.services.jellyfin.extras._installed_plugin_names", return_value=installed),
        patch("toolkit.services.jellyfin.extras._install_package") as install,
        patch("toolkit.services.jellyfin.extras._restart_jellyfin") as restart,
        patch("toolkit.services.jellyfin.extras._configure_jellyfin_ldap", return_value=(True, False)),
    ):
        logs = configure_jellyfin_extras(
            Config(domain="example.com"),
            "api-key",
            base_url="http://bridge:8096",
            lldap_bind_password="bind-password",
        )

    install.assert_not_called()
    restart.assert_not_called()
    assert "Jellyfin: plugin LDAP Authentication already installed" in logs


def test_ldap_configuration_uses_current_plugin_contract() -> None:
    current = MagicMock(returncode=1, stdout="")
    mkdir = MagicMock(returncode=0)
    write = MagicMock(returncode=0)
    with patch(
        "toolkit.services.jellyfin.extras.subprocess.run",
        side_effect=[mkdir, current, write],
    ) as run:
        ok, changed = _configure_jellyfin_ldap(
            Config(domain="example.com"),
            "api-key",
            base_url="http://bridge:8096",
            bind_password="bind-password",
        )

    assert ok and changed
    xml = run.call_args_list[2].kwargs["input"].decode()
    assert "<LdapBindUser>" in xml
    assert "<CreateUsersFromLdap>true</CreateUsersFromLdap>" in xml
    assert "<LdapUidAttribute>uid</LdapUidAttribute>" in xml
    assert "<LdapSearchFilter>(|(uid={username})(mail={username}))</LdapSearchFilter>" in xml
    assert "{0}" not in xml
    assert "<LdapBindDn>" not in xml
    assert "<CreateUserOnAuthentication>" not in xml
    assert run.call_args_list[1].args[0][-1] == LDAP_CONFIG_PATH
    assert run.call_args_list[2].args[0][-1] == LDAP_CONFIG_PATH


def test_jellyfin_extras_do_not_inject_remote_presentation_assets() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "toolkit/services/jellyfin/extras.py").read_text(encoding="utf-8")
    manifest = (root / "toolkit/services/jellyfin/service.yaml").read_text(encoding="utf-8")

    assert "CustomCss" not in source
    assert "cdn.jsdelivr.net" not in source
    assert "key: theme" not in manifest
