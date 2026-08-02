"""Jellyfin plugins and media-cache webhook post-wizard automation."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

JELLYFIN_OFFICIAL_REPO = "https://repo.jellyfin.org/files/plugin/manifest.json"
LDAP_PLUGIN_NAME = "LDAP Authentication"
LDAP_CONFIG_PATH = "/config/data/plugins/configurations/LDAP-Auth.xml"
INTRO_SKIPPER_REPO = "https://intro-skipper.org/manifest.json"
MERGE_VERSIONS_REPO = "https://raw.githubusercontent.com/danieladov/JellyfinPluginManifest/master/manifest.json"
WEBHOOK_PLUGIN_ID = "71552A5A-5C5C-4350-A2AE-EBE451A30173"
_PLUGIN_NAME_ALIASES = {
    LDAP_PLUGIN_NAME.lower(): frozenset({"ldap authentication", "ldap-auth", "ldap auth"}),
}


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Emby-Token": api_key, "Content-Type": "application/json"}


def _restart_jellyfin(base_url: str) -> bool:
    subprocess.run(["docker", "restart", "jellyfin"], check=False, timeout=90)
    for _ in range(40):
        try:
            if httpx.get(f"{base_url.rstrip('/')}/health", timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def _merge_repositories(base: str, api_key: str, extra: list[dict[str, str]]) -> tuple[bool, bool]:
    try:
        resp = httpx.get(f"{base}/Repositories", headers=_headers(api_key), timeout=15)
        existing = resp.json() if resp.status_code == 200 and isinstance(resp.json(), list) else []
    except httpx.HTTPError:
        existing = []
    by_url: dict[str, dict] = {}
    for item in existing:
        if isinstance(item, dict) and item.get("Url"):
            by_url[item["Url"]] = item
    changed = False
    for repo in extra:
        current = by_url.get(repo["Url"])
        if not current or current.get("Name") != repo["Name"] or not current.get("Enabled", True):
            changed = True
        by_url[repo["Url"]] = {
            "Name": repo["Name"],
            "Url": repo["Url"],
            "Enabled": True,
        }
    if not changed:
        return True, False
    try:
        post = httpx.post(f"{base}/Repositories", json=list(by_url.values()), headers=_headers(api_key), timeout=20)
        return post.status_code in (200, 201, 204), post.status_code in (200, 201, 204)
    except httpx.HTTPError:
        return False, False


def _installed_plugin_names(base: str, api_key: str) -> set[str]:
    try:
        response = httpx.get(f"{base}/Plugins", headers=_headers(api_key), timeout=15)
        if response.status_code != 200:
            return set()
        return {
            str(plugin.get("Name", "")).strip()
            for plugin in response.json()
            if isinstance(plugin, dict) and plugin.get("Name")
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return set()


def _install_package(base: str, api_key: str, name: str, repo_url: str) -> bool:
    try:
        resp = httpx.post(
            f"{base}/Packages/Installed/{quote(name)}",
            params={"repositoryUrl": repo_url},
            headers=_headers(api_key),
            timeout=120,
        )
        return resp.status_code in (200, 201, 204)
    except httpx.HTTPError:
        return False


def _plugin_active(base: str, api_key: str, plugin_id: str) -> bool:
    try:
        resp = httpx.get(f"{base}/Plugins", headers=_headers(api_key), timeout=15)
        if resp.status_code != 200:
            return False
        for item in resp.json():
            if not isinstance(item, dict):
                continue
            if str(item.get("Id", "")).lower() == plugin_id.lower():
                return bool(item.get("IsEnabled", True))
    except httpx.HTTPError:
        pass
    return False


def _configure_webhook(base: str, api_key: str, webhook_url: str, webhook_token: str = "") -> bool:
    if not _plugin_active(base, api_key, WEBHOOK_PLUGIN_ID):
        return False
    try:
        cfg_resp = httpx.get(
            f"{base}/Plugins/{WEBHOOK_PLUGIN_ID}/Configuration",
            headers=_headers(api_key),
            timeout=15,
        )
        if cfg_resp.status_code != 200:
            return False
        data = cfg_resp.json()
        if not isinstance(data, dict):
            return False
        options = data.get("GenericOptions")
        if not isinstance(options, list):
            options = []
        found = False

        def configure_auth_header(option: dict) -> None:
            if not webhook_token:
                return
            raw_headers = option.get("Headers")
            headers = list(raw_headers) if isinstance(raw_headers, list) else []
            headers = [
                header
                for header in headers
                if not (
                    isinstance(header, dict) and str(header.get("Key", "")).casefold() == "x-media-cache-webhook-token"
                )
            ]
            headers.append({"Key": "X-Media-Cache-Webhook-Token", "Value": webhook_token})
            option["Headers"] = headers

        for opt in options:
            if not isinstance(opt, dict):
                continue
            if opt.get("WebhookUri") == webhook_url:
                found = True
                opt["EnableWebhook"] = True
                opt.setdefault("NotificationTypes", ["PlaybackStart"])
                configure_auth_header(opt)
        if not found:
            option = {
                "Name": "media-cache",
                "WebhookUri": webhook_url,
                "EnableWebhook": True,
                "NotificationTypes": ["PlaybackStart"],
                "SendAllProperties": False,
            }
            configure_auth_header(option)
            options.append(option)
        data["GenericOptions"] = options
        post = httpx.post(
            f"{base}/Plugins/{WEBHOOK_PLUGIN_ID}/Configuration",
            json=data,
            headers=_headers(api_key),
            timeout=20,
        )
        return post.status_code in (200, 201, 204)
    except (httpx.HTTPError, ValueError):
        return False


def _lldap_server_host(config: Config) -> str:
    if config.is_multi_node:
        from toolkit.core.manifest.placement import service_address

        return service_address(config, "lldap")
    return "lldap"


def _configure_jellyfin_ldap(
    config: Config,
    api_key: str,
    *,
    base_url: str,
    bind_password: str,
) -> tuple[bool, bool]:
    """Point the installed LDAP Authentication plugin at infra LLDAP."""
    if not bind_password:
        return False, False

    from toolkit.services.sdk.ldap import base_dn as _ldap_base_dn
    from toolkit.services.sdk.ldap import bind_dn as _ldap_bind_dn
    from toolkit.services.sdk.ldap import ldap_host, lldap_group_ou, lldap_ldap_port, lldap_user_ou

    _base_dn = _ldap_base_dn(config)
    _people_dn = f"{lldap_user_ou()},{_base_dn}"
    _bind_dn = _ldap_bind_dn(config)
    _server = ldap_host(config)
    _port = str(lldap_ldap_port())

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<PluginConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <LdapServer>{_server}</LdapServer>
  <LdapPort>{_port}</LdapPort>
  <LdapBaseDn>{_people_dn}</LdapBaseDn>
  <LdapBindUser>{_bind_dn}</LdapBindUser>
  <LdapBindPassword>{bind_password}</LdapBindPassword>
  <LdapSearchFilter>(|(uid={{username}})(mail={{username}}))</LdapSearchFilter>
  <LdapAdminBaseDn>{lldap_group_ou()},{_base_dn}</LdapAdminBaseDn>
  <LdapAdminFilter>(memberOf=cn=homelab-admin,{lldap_group_ou()},{_base_dn})</LdapAdminFilter>
  <LdapSearchAttributes>uid, cn, mail, displayName</LdapSearchAttributes>
  <LdapUidAttribute>uid</LdapUidAttribute>
  <LdapUsernameAttribute>uid</LdapUsernameAttribute>
  <LdapPasswordAttribute>userPassword</LdapPasswordAttribute>
  <CreateUsersFromLdap>true</CreateUsersFromLdap>
  <AllowPassChange>false</AllowPassChange>
  <EnableAllFolders>true</EnableAllFolders>
  <UseSsl>false</UseSsl>
  <UseStartTls>false</UseStartTls>
  <SkipSslVerify>true</SkipSslVerify>
</PluginConfiguration>
"""
    proc_mkdir = subprocess.run(
        ["docker", "exec", "jellyfin", "mkdir", "-p", LDAP_CONFIG_PATH.rsplit("/", 1)[0]],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if proc_mkdir.returncode != 0:
        return False, False
    current = subprocess.run(
        ["docker", "exec", "jellyfin", "cat", LDAP_CONFIG_PATH],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if current.returncode == 0 and current.stdout.strip() == xml.strip():
        return True, False
    proc = subprocess.run(
        ["docker", "exec", "-i", "jellyfin", "tee", LDAP_CONFIG_PATH],
        input=xml.encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return False, False
    return True, True


def configure_jellyfin_extras(
    config: Config,
    api_key: str,
    *,
    base_url: str = "http://jellyfin:8096",
    lldap_bind_password: str = "",
    media_cache_webhook_token: str = "",
) -> list[str]:
    """Install recommended plugins and configure the media-cache webhook."""
    logs: list[str] = []
    from toolkit.core.manifest.settings import service_enabled

    cache_enabled = service_enabled(config, "media-cache")
    from toolkit.core.manifest.settings import service_setting_str

    if service_setting_str(config, "media-library", "server") not in ("jellyfin", "both"):
        return logs
    if not api_key:
        logs.append("Jellyfin extras: no API key — skip plugins")
        return logs

    extra_repos = [
        {"Name": "Intro Skipper", "Url": INTRO_SKIPPER_REPO},
        {"Name": "Merge Versions", "Url": MERGE_VERSIONS_REPO},
    ]
    repositories_ok, repositories_changed = _merge_repositories(base_url, api_key, extra_repos)
    if not repositories_ok:
        logs.append("Jellyfin: repository merge failed")
        return logs
    logs.append(
        "Jellyfin: plugin repositories updated" if repositories_changed else "Jellyfin: plugin repositories verified"
    )

    to_install: list[tuple[str, str]] = [
        ("Intro Skipper", INTRO_SKIPPER_REPO),
        ("Open Subtitles", JELLYFIN_OFFICIAL_REPO),
        ("TMDb Box Sets", JELLYFIN_OFFICIAL_REPO),
        ("Merge Versions", MERGE_VERSIONS_REPO),
    ]
    if cache_enabled:
        to_install.insert(0, ("Webhook", JELLYFIN_OFFICIAL_REPO))
    if lldap_bind_password:
        to_install.append((LDAP_PLUGIN_NAME, JELLYFIN_OFFICIAL_REPO))

    installed_names = {name.lower() for name in _installed_plugin_names(base_url, api_key)}
    installed_any = False
    for pkg, repo in to_install:
        accepted_names = _PLUGIN_NAME_ALIASES.get(pkg.lower(), frozenset({pkg.lower()}))
        if installed_names.intersection(accepted_names):
            logs.append(f"Jellyfin: plugin {pkg} already installed")
            continue
        if _install_package(base_url, api_key, pkg, repo):
            logs.append(f"Jellyfin: installed plugin {pkg}")
            installed_any = True
        else:
            logs.append(f"Jellyfin: plugin {pkg} install skipped or already present")

    if installed_any:
        if not _restart_jellyfin(base_url):
            logs.append("Jellyfin: restart timed out after plugin installation")
            return logs

    if cache_enabled:
        if _configure_webhook(
            base_url,
            api_key,
            "http://media-cache:8686/webhook/jellyfin",
            media_cache_webhook_token,
        ):
            logs.append("Jellyfin: media-cache webhook configured")
        else:
            logs.append("Jellyfin: media-cache webhook not configured (install Webhook plugin first)")

    if lldap_bind_password:
        ldap_ok, ldap_changed = _configure_jellyfin_ldap(
            config, api_key, base_url=base_url, bind_password=lldap_bind_password
        )
        if ldap_ok:
            logs.append(
                "Jellyfin: LDAP authentication configured (LLDAP users)"
                if ldap_changed
                else "Jellyfin: LDAP authentication verified"
            )
            if ldap_changed and not _restart_jellyfin(base_url):
                logs.append("Jellyfin: restart timed out after LDAP configuration")
        else:
            logs.append("Jellyfin: LDAP plugin install/config skipped or failed")

    return logs
