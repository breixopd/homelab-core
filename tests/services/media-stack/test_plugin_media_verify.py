"""Unit tests for media-stack plugin verify() deepening."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin(service: str):
    module = load_plugin(service)
    class_name = "".join(part.title() for part in service.split("-")) + "Plugin"
    cls = getattr(module, class_name, None)
    if isinstance(cls, type):
        return cls()
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and name.endswith("Plugin") and name != "ServicePlugin":
            return obj()
    raise RuntimeError(f"no plugin class in {service}")


def _cfg(**media_kw) -> Config:
    cache_enabled = bool(media_kw.pop("cache", False))
    service_settings: dict[str, dict[str, object]] = {
        "media-library": {"server": media_kw.pop("server", "both")},
        "gluetun": {"enabled": media_kw.pop("vpn", True)},
        "tdarr": {"enabled": media_kw.pop("tdarr", True)},
        "media-cache": {"enabled": cache_enabled},
    }
    if "hw_transcode" in media_kw:
        service_settings["jellyfin"] = {"hardware-transcode": media_kw.pop("hw_transcode")}
    assert not media_kw, f"unmapped media settings: {media_kw}"
    return Config(
        domain="example.com",
        services=ServicesConfig(media=True),
        service_settings=service_settings,
    )


class TestSonarrVerify:
    def test_health_and_indexers(self, tmp_path, monkeypatch):
        cfg = _cfg()
        health = []
        indexers = [
            {
                "enable": True,
                "name": "Prowlarr-1337x",
                "fields": [{"name": "baseUrl", "value": "http://prowlarr:9696/1/"}],
            }
        ]

        def fake_get(path):
            resp = MagicMock()
            resp.status_code = 200
            if path.endswith("/health"):
                resp.json.return_value = health
            elif path.endswith("/rootFolder"):
                resp.json.return_value = [{"path": "/data/tv"}]
            elif path.endswith("/downloadclient"):
                resp.json.return_value = [{"implementation": "QBittorrent", "id": 1}]
            elif path.endswith("/indexer"):
                resp.json.return_value = indexers
            return resp

        monkeypatch.setattr("toolkit.services._arr.servarr_get", lambda *_a, **_k: lambda path: fake_get(path))
        monkeypatch.setattr(
            "toolkit.services._arr.verify_arr_downloadclient_test",
            lambda *_a, **_k: type("C", (), {"check": "download_client_test", "passed": True, "detail": "ok"})(),
        )
        from toolkit.services.sdk import VerifyCheck

        monkeypatch.setattr(
            "toolkit.services._arr.verify_arr_downloadclient_test",
            lambda *_a, **_k: VerifyCheck("sonarr", "download_client_test", True, "test passed"),
        )

        checks = {c.check: c for c in _plugin("sonarr").verify(cfg, {"SONARR_API_KEY": "k"}, "10.10.10.11", tmp_path)}
        assert checks["health"].passed is True
        assert checks["indexers"].passed is True

    def test_health_errors_fail(self, tmp_path, monkeypatch):
        cfg = _cfg()

        def fake_get(path):
            resp = MagicMock()
            resp.status_code = 200
            if path.endswith("/health"):
                resp.json.return_value = [{"type": "IndexerStatusCheck", "severity": "error", "message": "down"}]
            elif path.endswith("/rootFolder"):
                resp.json.return_value = []
            elif path.endswith("/downloadclient"):
                resp.json.return_value = []
            elif path.endswith("/indexer"):
                resp.json.return_value = []
            return resp

        monkeypatch.setattr("toolkit.services._arr.servarr_get", lambda *_a, **_k: lambda path: fake_get(path))
        from toolkit.services.sdk import VerifyCheck

        monkeypatch.setattr(
            "toolkit.services._arr.verify_arr_downloadclient_test",
            lambda *_a, **_k: VerifyCheck("sonarr", "download_client_test", False, "fail"),
        )
        checks = {c.check: c for c in _plugin("sonarr").verify(cfg, {"SONARR_API_KEY": "k"}, "10.10.10.11", tmp_path)}
        assert checks["health"].passed is False
        assert "IndexerStatusCheck" in checks["health"].detail


class TestRadarrVerify:
    def test_delegates_to_arr_standard(self, tmp_path, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr(
            "toolkit.services._arr.verify_arr_standard",
            lambda *a, **k: [type("C", (), {"check": "health", "passed": True, "detail": "ok"})()],
        )
        checks = _plugin("radarr").verify(cfg, {"RADARR_API_KEY": "k"}, "10.10.10.11", tmp_path)
        assert checks[0].passed is True


class TestProwlarrVerify:
    def test_applications_full_sync(self, tmp_path, monkeypatch):
        cfg = _cfg()

        def fake_get(path):
            resp = MagicMock()
            resp.status_code = 200
            if path.endswith("/health"):
                resp.json.return_value = []
            elif path.endswith("/indexer"):
                resp.json.return_value = [{"enable": True, "name": "1337x", "definitionName": "1337x"}]
            elif path.endswith("/applications"):
                resp.json.return_value = [
                    {"name": "Sonarr", "syncLevel": "fullSync"},
                    {"name": "Radarr", "syncLevel": "fullSync"},
                ]
            return resp

        monkeypatch.setattr("toolkit.services._arr.servarr_get", lambda *_a, **_k: lambda path: fake_get(path))
        checks = {
            c.check: c for c in _plugin("prowlarr").verify(cfg, {"PROWLARR_API_KEY": "k"}, "10.10.10.11", tmp_path)
        }
        assert checks["applications"].passed is True


class TestBazarrVerify:
    def test_providers_and_links(self, tmp_path, monkeypatch):
        cfg = _cfg()
        settings = {
            "general": {"languages": "eng", "enabled_providers": ["embeddedsubtitles"]},
            "subsync": {"use_subsync": True},
            "sonarr": {"ip": "sonarr", "apikey": "s"},
            "radarr": {"ip": "radarr", "apikey": "r"},
        }
        monkeypatch.setattr("toolkit.services._arr.resolve_bazarr_api_key", lambda *_a, **_k: "key")
        monkeypatch.setattr("toolkit.services._arr._bazarr_settings", lambda *_a, **_k: settings)
        monkeypatch.setattr(
            "toolkit.services._arr.verify_bazarr_health",
            lambda *_a, **_k: type("C", (), {"check": "health", "passed": True, "detail": "ok"})(),
        )
        from toolkit.services.sdk import VerifyCheck

        monkeypatch.setattr(
            "toolkit.services._arr.verify_bazarr_health",
            lambda *_a, **_k: VerifyCheck("bazarr", "health", True, "ok"),
        )
        checks = {c.check: c for c in _plugin("bazarr").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["providers"].passed is True
        assert checks["arr_links"].passed is True


class TestJellyfinVerify:
    def test_ldap_without_admin_credentials_is_not_ready(self, tmp_path):
        plugin = _plugin("jellyfin")

        check = plugin._check_ldap_plugin_active(_cfg(), {}, "10.10.10.11", tmp_path, lambda *_args: "")

        assert check.passed is False
        assert check.status.value == "not_ready"

    def test_health_endpoint(self, tmp_path, monkeypatch):
        cfg = _cfg()
        plugin = _plugin("jellyfin")
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, '{"status":"Healthy"}'))
        monkeypatch.setattr(
            plugin,
            "_check_libraries",
            lambda *_a, **_k: type("C", (), {"check": "libraries", "passed": True, "detail": "1"})(),
        )
        monkeypatch.setattr(
            plugin,
            "_check_ldap",
            lambda *_a, **_k: type("C", (), {"check": "ldap", "passed": True, "detail": "ok"})(),
        )
        monkeypatch.setattr(
            plugin,
            "_check_plugins",
            lambda *_a, **_k: type("C", (), {"check": "plugins", "passed": True, "detail": "ok"})(),
        )
        monkeypatch.setattr(
            plugin,
            "_check_ldap_plugin_active",
            lambda *_a, **_k: type("C", (), {"check": "ldap_active", "passed": True, "detail": "ok"})(),
        )
        monkeypatch.setattr(
            plugin,
            "_check_hw_transcode",
            lambda *_a, **_k: type("C", (), {"check": "hw_transcode", "passed": True, "detail": "skip"})(),
        )
        checks = {c.check: c for c in plugin.verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["health"].passed is True


class TestTautulliVerify:
    def test_missing_api_key_is_not_ready(self, tmp_path):
        checks = _plugin("tautulli").verify(_cfg(), {}, "10.10.10.11", tmp_path)

        assert checks[0].passed is False
        assert checks[0].status.value == "not_ready"

    def test_requires_configured_plex_server(self, tmp_path, monkeypatch):
        payload = '{"response":{"result":"success","data":{}}}'
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, payload))

        checks = _plugin("tautulli").verify(_cfg(), {"TAUTULLI_API_KEY": "key"}, "10.10.10.11", tmp_path)

        assert checks[0].passed is False

    def test_authenticated_server_info_passes(self, tmp_path, monkeypatch):
        payload = '{"response":{"result":"success","data":{"pms_identifier":"plex-1"}}}'
        requests = []

        def fake_curl(_cfg, _ip, _container, url, **_kwargs):
            requests.append(url)
            return 0, payload

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        checks = _plugin("tautulli").verify(_cfg(), {"TAUTULLI_API_KEY": "key"}, "10.10.10.11", tmp_path)

        assert checks[0].passed is True
        assert "apikey=key" in requests[0]

    def test_plugin_inventory_uses_secure_authenticated_requests(self, tmp_path, monkeypatch):
        cfg = _cfg()
        requests = []

        def fake_request(_cfg, _vm_ip, _container, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("/AuthenticateByName"):
                return 0, '{"AccessToken":"test-access-token"}'
            if url.endswith("/Plugins"):
                return 0, '[{"Name":"LDAP-Auth","Status":"Active"}]'
            return 1, ""

        def fake_exec(_cfg, _container, command, _vm_ip, _root, **_kwargs):
            if command[-1].endswith("AuthenticateByName"):
                return 0, '{"AccessToken":"test-access-token"}'
            return 0, '[{"Name":"LDAP-Auth","Status":"Active"}]\nHTTP:200'

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        plugins = _plugin("jellyfin")._fetch_plugins(
            cfg,
            {"JELLYFIN_ADMIN_PASSWORD": "test-admin-password"},
            "10.10.10.11",
            tmp_path,
            lambda secrets, name: secrets[name],
        )

        assert plugins == [{"Name": "LDAP-Auth", "Status": "Active"}]
        assert [url.rsplit("/", 1)[-1] for url, _kwargs in requests] == ["AuthenticateByName", "Plugins"]
        assert requests[0][1]["method"] == "POST"
        assert requests[0][1]["body"] == '{"Username": "admin", "Pw": "test-admin-password"}'
        assert requests[1][1]["headers"]["X-Emby-Token"] == "test-access-token"


class TestPlexVerify:
    def test_missing_token_is_not_ready_not_pass(self, tmp_path):
        checks = _plugin("plex").verify(_cfg(), {}, "10.10.10.11", tmp_path)

        assert len(checks) == 1
        assert checks[0].check == "identity"
        assert checks[0].passed is False
        assert checks[0].status.value == "not_ready"

    def test_identity_and_libraries(self, tmp_path, monkeypatch):
        cfg = _cfg()
        calls = []

        def fake_curl(_cfg, _ip, container, url, **_kw):
            calls.append(url)
            if "/identity" in url:
                return 0, '<MediaContainer machineIdentifier="abc123"/>'
            return 0, "<MediaContainer><Directory/></MediaContainer>"

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        checks = {c.check: c for c in _plugin("plex").verify(cfg, {"PLEX_TOKEN": "tok"}, "10.10.10.11", tmp_path)}
        assert checks["identity"].passed is True
        assert checks["libraries"].passed is True


class TestQbittorrentVerify:
    def test_auth_probe_delimits_multi_network_addresses(self, tmp_path, monkeypatch):
        # Load the concrete module so later monkeypatch dotted paths resolve
        # even though the test helper uses dynamic plugin loading.
        import importlib

        importlib.import_module("toolkit.services.qbittorrent.plugin")
        cfg = _cfg(vpn=True)
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        inspect_commands: list[str] = []

        def fake_ssh(_cfg, _ip, command, **_kwargs):
            inspect_commands.append(command)
            if "NetworkMode" in command:
                return 0, "container:gluetun\n", ""
            if "NetworkSettings.Networks" in command:
                return 0, "172.31.20.4 172.31.30.4\n", ""
            return 0, "403", ""

        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)
        check = _plugin("qbittorrent")._verify_auth(cfg, "10.10.10.11", tmp_path)

        assert check.passed is True
        network_inspect = next(command for command in inspect_commands if "NetworkSettings.Networks" in command)
        assert "{{.IPAddress}} {{end}}" in network_inspect

    def test_vpn_skipped_when_off(self, tmp_path, monkeypatch):
        cfg = _cfg(vpn=False)
        monkeypatch.setattr(
            "toolkit.services.qbittorrent.plugin.QbittorrentPlugin._verify_auth",
            lambda *_a, **_k: type("C", (), {"check": "auth", "passed": True, "detail": "ok"})(),
        )
        monkeypatch.setattr(
            "toolkit.services.qbittorrent.plugin.QbittorrentPlugin._check_authenticated_api",
            lambda *_a, **_k: [],
        )
        checks = {c.check: c for c in _plugin("qbittorrent").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["vpn_egress"].passed is True
        assert "skipped" in checks["vpn_egress"].detail

    def test_authenticated_api_keeps_webui_credentials_in_request_config(self, tmp_path, monkeypatch):
        cfg = _cfg(vpn=False)
        requests = []

        def fake_request(_cfg, _vm_ip, _container, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("/auth/login"):
                return 0, "Ok."
            if url.endswith("/app/preferences"):
                return 0, json.dumps({"save_path": "/data/downloads"})
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, json.dumps({"save_path": "/data/downloads"})),
        )

        checks = _plugin("qbittorrent")._check_authenticated_api(
            cfg,
            {"QBITTORRENT_USER": "admin", "QBITTORRENT_PASSWORD": "test-webui-password"},
            "10.10.10.11",
            tmp_path,
        )

        assert all(check.passed for check in checks)
        assert [url.rsplit("/", 1)[-1] for url, _kwargs in requests] == ["login", "preferences"]
        assert requests[0][1]["body"] == "username=admin&password=test-webui-password"
        assert requests[0][1]["cookie_jar"] == "/tmp/qbit-cookie"
        assert requests[1][1]["cookie_file"] == "/tmp/qbit-cookie"


class TestGluetunVerify:
    def test_skips_when_vpn_off(self, tmp_path):
        cfg = _cfg(vpn=False)
        checks = _plugin("gluetun").verify(cfg, {}, "10.10.10.11", tmp_path)
        assert checks[0].passed is True


class TestFlaresolverrVerify:
    def test_health_only_on_localhost(self, tmp_path, monkeypatch):
        cfg = Config(domain="localhost", services=ServicesConfig(media=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, json.dumps({"status": "ok"})),
        )
        checks = {c.check: c for c in _plugin("flaresolverr").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["health"].passed is True
        assert "solve" not in checks


class TestRecyclarrVerify:
    def test_radarr_profiles(self, tmp_path, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "ok", ""),
        )
        profiles = [{"name": "WEB-1080p"}]

        requests = []

        def fake_request(_cfg, _vm_ip, container, url, **kwargs):
            requests.append((container, url, kwargs))
            if container == "sonarr":
                return 0, json.dumps(profiles)
            if container == "radarr":
                return 0, json.dumps(profiles)
            return 0, ""

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("raw request commands are forbidden")),
        )
        checks = {
            c.check: c
            for c in _plugin("recyclarr").verify(
                cfg, {"SONARR_API_KEY": "s", "RADARR_API_KEY": "r"}, "10.10.10.11", tmp_path
            )
        }
        assert checks["profiles"].passed is True
        assert checks["radarr_profiles"].passed is True
        assert {container for container, _url, _kwargs in requests} == {"sonarr", "radarr"}
        assert all("SONARR_API_KEY" not in url and "RADARR_API_KEY" not in url for _container, url, _kwargs in requests)
        assert {kwargs["headers"]["X-Api-Key"] for _container, _url, kwargs in requests} == {"s", "r"}


class TestNavidromeVerify:
    def test_ping_and_library(self, tmp_path, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))

        def fake_exec(cfg, container, cmd, vm_ip, root, timeout=20):
            if "find" in str(cmd):
                return 0, "0"
            if "ls -A" in str(cmd):
                return 0, ""
            return (
                0,
                "ND_EXTAUTH_TRUSTEDSOURCES=10.10.10.10/32\n"
                "ND_EXTAUTH_USERHEADER=Remote-User\n"
                "ND_ENABLEUSEREDITING=false\n",
            )

        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        checks = {c.check: c for c in _plugin("navidrome").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["ping"].passed is True
        assert checks["external_auth"].passed is True
        assert checks["library"].passed is True
        assert "empty" in checks["library"].detail

    def test_external_auth_uses_the_manifest_caddy_owner(self, tmp_path, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, "ok"))
        monkeypatch.setattr(
            "toolkit.core.manifest.placement.service_address",
            lambda _cfg, service: "192.0.2.44" if service == "caddy" else "",
        )

        def fake_exec(cfg, container, cmd, vm_ip, root, timeout=20):
            if "find" in str(cmd):
                return 0, "0"
            if "ls -A" in str(cmd):
                return 0, ""
            return (
                0,
                "ND_EXTAUTH_TRUSTEDSOURCES=192.0.2.44/32\n"
                "ND_EXTAUTH_USERHEADER=Remote-User\n"
                "ND_ENABLEUSEREDITING=false\n",
            )

        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)

        checks = {c.check: c for c in _plugin("navidrome").verify(cfg, {}, "192.0.2.45", tmp_path)}

        assert checks["external_auth"].passed is True


class TestSeerrVerify:
    def test_status_and_connections(self, tmp_path, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/status"):
                return 0, json.dumps({"version": "2.0.0"})
            if url.endswith("/settings/public"):
                return 0, json.dumps({"initialized": True})
            if url.endswith("/settings/jellyfin"):
                return 0, json.dumps({"hostname": "jellyfin", "serverID": "abc"})
            if url.endswith("/settings/sonarr"):
                return 0, json.dumps([{"name": "Sonarr", "hostname": "sonarr", "apiKey": "configured"}])
            if url.endswith("/settings/radarr"):
                return 0, json.dumps([{"name": "Radarr", "hostname": "radarr", "apiKey": "configured"}])
            return 0, "{}"

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services._arr.resolve_seerr_api_key", lambda *_a, **_k: "key")
        checks = {c.check: c for c in _plugin("seerr").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["status"].passed is True
        assert checks["connections"].passed is True


class TestTdarrVerify:
    def test_nodes_count(self, tmp_path, monkeypatch):
        cfg = _cfg(tdarr=True)
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

        def fake_curl(_cfg, _ip, container, url, **kwargs):
            body = kwargs.get("body", "")
            if "NodeJSONDB" in body:
                return 0, json.dumps([{"id": "n1"}])
            if "FlowsJSONDB" in body:
                return 0, json.dumps([{"name": "Homelab flow"}])
            if "search-flow-templates" in url:
                return 0, json.dumps([[{"name": "Community template"}], "Community"])
            return 0, json.dumps({"status": "ok"})

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "", ""),
        )
        checks = {c.check: c for c in _plugin("tdarr").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["nodes"].passed is True

    def test_skips_when_container_missing(self, tmp_path, monkeypatch):
        cfg = _cfg(tdarr=True)
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = {c.check: c for c in _plugin("tdarr").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["server"].passed is False
        assert checks["server"].detail == "container missing"


class TestMediaCacheVerify:
    def test_health_and_status(self, tmp_path, monkeypatch):
        cfg = _cfg(cache=True)
        cfg.external_hosts = [type("H", (), {"services": ["media-cache"]})()]

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/health"):
                return 0, json.dumps({"status": "ok"})
            if url.endswith("/api/status"):
                return 0, json.dumps({"cache_max_gb": 500, "tracked_files": 3})
            return 0, json.dumps({"backends": ["nas1"]})

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        checks = {c.check: c for c in _plugin("media-cache").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["health"].passed is True
        assert checks["cache_status"].passed is True
        assert checks["backends"].passed is True
