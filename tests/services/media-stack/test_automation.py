from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from toolkit.services._arr import extract_servarr_api_key, wire_bazarr_providers, wire_seerr_arr
from toolkit.services.jellyfin.bootstrap import setup_jellyfin_api_key

# ---------------------------------------------------------------------------
# extract_servarr_api_key
# ---------------------------------------------------------------------------


class TestExtractServarrApiKey:
    def test_returns_api_key_when_found(self, tmp_path):
        config = tmp_path / "config.xml"
        config.write_text('<?xml version="1.0"?>\n<Config>\n  <ApiKey>abc123secretkey</ApiKey>\n</Config>')
        result = extract_servarr_api_key(config)
        assert result == "abc123secretkey"

    def test_returns_none_when_api_key_absent(self, tmp_path):
        config = tmp_path / "config.xml"
        config.write_text('<?xml version="1.0"?>\n<Config>\n  <SomeOtherKey>value</SomeOtherKey>\n</Config>')
        result = extract_servarr_api_key(config)
        assert result is None

    def test_returns_none_when_api_key_empty(self, tmp_path):
        config = tmp_path / "config.xml"
        config.write_text('<?xml version="1.0"?>\n<Config>\n  <ApiKey></ApiKey>\n</Config>')
        result = extract_servarr_api_key(config)
        assert result is None

    def test_returns_stripped_key(self, tmp_path):
        config = tmp_path / "config.xml"
        config.write_text('<?xml version="1.0"?>\n<Config>\n  <ApiKey>  spacesAround  </ApiKey>\n</Config>')
        result = extract_servarr_api_key(config)
        assert result == "spacesAround"

    def test_accepts_string_path(self, tmp_path):
        config = tmp_path / "config.xml"
        config.write_text('<?xml version="1.0"?>\n<Config>\n  <ApiKey>strpathkey</ApiKey>\n</Config>')
        result = extract_servarr_api_key(str(config))
        assert result == "strpathkey"

    def test_returns_none_for_missing_file(self):
        result = extract_servarr_api_key("/nonexistent/path/config.xml")
        assert result is None

    def test_returns_none_on_malformed_xml(self, tmp_path):
        config = tmp_path / "bad.xml"
        config.write_text("not xml at all >>>")
        result = extract_servarr_api_key(config)
        assert result is None

    def test_rejects_entity_declarations(self, tmp_path):
        config = tmp_path / "hostile.xml"
        config.write_text(
            '<!DOCTYPE Config [<!ENTITY key "expanded">]><Config><ApiKey>&key;</ApiKey></Config>',
            encoding="utf-8",
        )

        assert extract_servarr_api_key(config) is None


class TestBazarrProviders:
    def test_existing_provider_set_does_not_post(self):
        current = MagicMock(
            content=b"{}",
            json=lambda: {
                "general": {"enabled_providers": "embeddedsubtitles"},
                "subsync": {"use_subsync": True},
            },
        )
        current.raise_for_status.return_value = None

        with (
            patch("toolkit.services._arr.httpx.get", return_value=current),
            patch("toolkit.services._arr.httpx.post") as post,
        ):
            logs = wire_bazarr_providers("http://bazarr:6767", "key")

        post.assert_not_called()
        assert logs == ["Bazarr: providers and Subsync already configured (embeddedsubtitles)"]

    def test_failed_provider_update_is_blocking_compatible(self):
        current = MagicMock(
            content=b"",
            json=lambda: {"general": {"enabled_providers": ""}, "subsync": {"use_subsync": False}},
        )
        current.raise_for_status.return_value = None
        failed = MagicMock(status_code=500)

        with (
            patch("toolkit.services._arr.httpx.get", return_value=current),
            patch("toolkit.services._arr.httpx.post", return_value=failed) as post,
        ):
            logs = wire_bazarr_providers("http://bazarr:6767", "key")

        assert logs == ["Bazarr: failed provider update (HTTP 500)"]
        assert post.call_args.kwargs["data"]["settings-subsync-use_subsync"] == "true"

    def test_absent_opensubtitlesorg_section_is_not_fabricated(self):
        current = MagicMock(
            content=b"{}",
            json=lambda: {
                "general": {"enabled_providers": "embeddedsubtitles"},
                "subsync": {"use_subsync": True},
            },
        )
        current.raise_for_status.return_value = None
        with (
            patch("toolkit.services._arr.httpx.get", return_value=current),
            patch("toolkit.services._arr.httpx.post") as post,
        ):
            logs = wire_bazarr_providers("http://bazarr:6767", "key", flaresolverr_url="http://flare:8191")

        post.assert_not_called()
        assert logs == ["Bazarr: providers and Subsync already configured (embeddedsubtitles)"]


class TestSeerrWiring:
    def test_jellyfin_bootstrap_retries_transient_failure(self):
        def get(url, **_kwargs):
            response = MagicMock(status_code=200)
            if url.endswith("/settings/public"):
                response.json.return_value = {"initialized": False}
            elif url.endswith("/settings/sonarr"):
                response.json.return_value = [{"name": "Sonarr"}]
            elif url.endswith("/settings/radarr"):
                response.json.return_value = [{"name": "Radarr"}]
            return response

        unavailable = MagicMock(status_code=500)
        authenticated = MagicMock(status_code=200)
        initialized = MagicMock(status_code=200)
        with (
            patch("toolkit.services._arr.httpx.get", side_effect=get),
            patch(
                "toolkit.services._arr.httpx.post",
                side_effect=[unavailable, authenticated, initialized],
            ) as post,
            patch("toolkit.services._arr.time.sleep") as sleep,
        ):
            logs = wire_seerr_arr(
                "http://seerr:5055",
                "seerr-key",
                "http://sonarr:8989",
                "sonarr-key",
                "http://radarr:7878",
                "radarr-key",
                jellyfin_url="http://jellyfin:8096",
                jellyfin_user="admin",
                jellyfin_password="secret",
            )

        assert "Seerr: admin account created from Jellyfin" in logs
        assert post.call_count >= 2
        sleep.assert_called_once_with(5)

    def test_failed_jellyfin_bootstrap_defers_dependent_configuration(self):
        public = MagicMock(status_code=200)
        public.json.return_value = {"initialized": False}
        failed = MagicMock(status_code=401)
        with (
            patch("toolkit.services._arr.httpx.get", return_value=public),
            patch("toolkit.services._arr.httpx.post", return_value=failed) as post,
        ):
            logs = wire_seerr_arr(
                "http://seerr:5055",
                "seerr-key",
                "http://sonarr:8989",
                "sonarr-key",
                "http://radarr:7878",
                "radarr-key",
                jellyfin_url="http://jellyfin:8096",
                jellyfin_user="admin",
                jellyfin_password="wrong",
            )

        assert logs[-1] == "Seerr: setup deferred until media server authentication succeeds"
        assert post.call_count == 1

    def test_existing_jellyfin_link_is_not_reposted(self):
        def get(url, **_kwargs):
            response = MagicMock(status_code=200)
            if url.endswith("/settings/public"):
                response.json.return_value = {"initialized": True}
            elif url.endswith("/settings/jellyfin"):
                response.json.return_value = {"hostname": "jellyfin", "serverID": "server-1"}
            elif url.endswith("/settings/sonarr"):
                response.json.return_value = [{"name": "Sonarr"}]
            elif url.endswith("/settings/radarr"):
                response.json.return_value = [{"name": "Radarr"}]
            return response

        initialized = MagicMock(status_code=200)
        with (
            patch("toolkit.services._arr.httpx.get", side_effect=get),
            patch("toolkit.services._arr.httpx.post", return_value=initialized) as post,
        ):
            logs = wire_seerr_arr(
                "http://seerr:5055",
                "seerr-key",
                "http://sonarr:8989",
                "sonarr-key",
                "http://radarr:7878",
                "radarr-key",
                jellyfin_url="http://jellyfin:8096",
                jellyfin_api_key="jellyfin-key",
            )

        assert "Seerr: Jellyfin server already linked" in logs
        assert not any(call.args[0].endswith("/settings/jellyfin") for call in post.call_args_list)


# ---------------------------------------------------------------------------
# setup_jellyfin_api_key
# ---------------------------------------------------------------------------


class TestSetupJellyfinApiKey:
    def _make_resp(self, json_data=None, raise_status=None, status_code=200):
        """Helper to build a mock httpx.Response."""
        r = MagicMock(spec=httpx.Response)
        r.status_code = status_code
        r.json.return_value = json_data or {}
        if raise_status:
            r.raise_for_status.side_effect = raise_status
        else:
            r.raise_for_status.return_value = None
        return r

    def test_returns_api_key_on_success(self):
        auth_resp = self._make_resp({"AccessToken": "test-access-token"})
        keys_resp = self._make_resp({"Items": [{"AppName": "homelab-toolkit", "AccessToken": "generated-api-key-xyz"}]})

        with patch("httpx.post", return_value=auth_resp), patch("httpx.get", return_value=keys_resp):
            result = setup_jellyfin_api_key(
                base_url="http://jellyfin:8096",
                admin_user="admin",
                admin_pass="secret",
            )
        assert result == "generated-api-key-xyz"

    def test_reuses_existing_named_key_without_creating_another(self):
        auth_resp = self._make_resp({"AccessToken": "test-access-token"})
        keys_resp = self._make_resp({"Items": [{"AppName": "homelab-toolkit", "AccessToken": "stable-key"}]})

        with patch("httpx.post", return_value=auth_resp) as post, patch("httpx.get", return_value=keys_resp):
            result = setup_jellyfin_api_key()

        assert result == "stable-key"
        post.assert_called_once()

    def test_returns_none_when_access_token_missing(self):
        resp = self._make_resp({"AccessToken": ""})
        with patch("httpx.post", return_value=resp):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_returns_none_on_auth_http_error(self):
        resp = self._make_resp(raise_status=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock()))
        with patch("httpx.post", return_value=resp):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_returns_none_on_key_creation_http_error(self):
        auth_resp = self._make_resp({"AccessToken": "token-ok"})
        key_resp = self._make_resp(raise_status=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock()))

        with (
            patch("httpx.post", side_effect=[auth_resp, key_resp]),
            patch("httpx.get", return_value=self._make_resp({})),
        ):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_returns_none_when_key_not_in_list(self):
        auth_resp = self._make_resp({"AccessToken": "token-ok"})
        key_resp = self._make_resp(status_code=204)
        keys_resp = self._make_resp({"Items": []})

        with (
            patch("httpx.post", side_effect=[auth_resp, key_resp]),
            patch("httpx.get", return_value=keys_resp),
        ):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_returns_none_on_connection_error(self):
        with patch("httpx.post", side_effect=httpx.ConnectError("timeout")):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_returns_none_on_http_error(self):
        with patch("httpx.post", side_effect=httpx.HTTPError("boom")):
            result = setup_jellyfin_api_key()
        assert result is None

    def test_uses_custom_key_name(self):
        auth_resp = self._make_resp({"AccessToken": "token-ok"})
        keys_resp = self._make_resp({"Items": [{"AppName": "my-custom-key", "AccessToken": "custom-key-123"}]})

        with patch("httpx.post", return_value=auth_resp), patch("httpx.get", return_value=keys_resp):
            result = setup_jellyfin_api_key(key_name="my-custom-key")
        assert result == "custom-key-123"
