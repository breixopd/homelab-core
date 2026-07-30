"""Unit tests for ntfy client and *arr notification wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from toolkit.services._arr import (
    wire_arr_notifications,
    wire_prowlarr_apps,
)
from toolkit.services.ntfy.client import NtfyClient


class TestNtfyClientHealth:
    def test_health_returns_true_on_200(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            assert client.health() is True

    def test_health_returns_false_on_non_200(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_get.return_value = mock_resp
            assert client.health() is False

    def test_health_returns_false_on_httperror(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.get") as mock_get:
            mock_get.side_effect = httpx.HTTPError("connection failed")
            assert client.health() is False


class TestNtfyClientSend:
    def test_send_returns_true_on_success(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            assert client.send("topic", "message") is True

    def test_send_returns_true_on_created(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_post.return_value = mock_resp
            assert client.send("topic", "message") is True

    def test_send_returns_false_on_httperror(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.post") as mock_post:
            mock_post.side_effect = httpx.HTTPError("connection failed")
            assert client.send("topic", "message") is False

    def test_send_includes_title_header_when_provided(self):
        client = NtfyClient()
        with patch("toolkit.services.ntfy.client.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            client.send("topic", "msg", title="Test Title")
            _, kwargs = mock_post.call_args
            assert kwargs["headers"]["Title"] == "Test Title"


class TestWireArrNotifications:
    def test_wire_arr_notifications_returns_true_on_success(self):
        schema = {
            "name": "",
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "fields": [
                {"name": "url", "value": None},
                {"name": "method", "value": 1},
                {"name": "username", "value": None},
                {"name": "password", "value": None},
                {"name": "headers", "value": []},
            ],
            "onGrab": False,
            "onDownload": False,
            "onUpgrade": False,
        }
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.post") as mock_post,
        ):
            existing = MagicMock(status_code=200)
            existing.json.return_value = []
            schemas = MagicMock(status_code=200)
            schemas.json.return_value = [schema]
            mock_get.side_effect = [existing, schemas]

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 201
            mock_post.return_value = mock_post_resp
            result = wire_arr_notifications(
                arr_url="http://sonarr:8989",
                arr_api_key="abc123",
                ntfy_url="http://ntfy:80",
                ntfy_topic="test",
            )
            assert result is True
            payload = mock_post.call_args.kwargs["json"]
            assert {field["name"] for field in payload["fields"]} == {
                "url",
                "method",
                "username",
                "password",
                "headers",
            }
            assert next(field["value"] for field in payload["fields"] if field["name"] == "url") == (
                "http://ntfy:80/test"
            )

    def test_wire_arr_notifications_reconciles_existing_configuration(self):
        current = {
            "id": 12,
            "name": "ntfy",
            "fields": [{"name": "url", "value": "http://old/topic"}, {"name": "method", "value": 1}],
            "onGrab": True,
            "onDownload": True,
            "onUpgrade": True,
        }
        schema = {
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "fields": [{"name": "url", "value": None}, {"name": "method", "value": 1}],
        }
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.put") as mock_put,
        ):
            existing = MagicMock(status_code=200)
            existing.json.return_value = [current]
            schemas = MagicMock(status_code=200)
            schemas.json.return_value = [schema]
            mock_get.side_effect = [existing, schemas]
            mock_put.return_value = MagicMock(status_code=202)

            assert wire_arr_notifications("http://sonarr:8989", "key", "http://ntfy:80/", "updated") is True

        assert mock_put.call_args.args[0].endswith("/api/v3/notification/12")
        payload = mock_put.call_args.kwargs["json"]
        assert payload["id"] == 12
        assert next(field["value"] for field in payload["fields"] if field["name"] == "url") == (
            "http://ntfy:80/updated"
        )

    def test_wire_arr_notifications_returns_false_on_httperror(self):
        with patch("toolkit.services._arr.httpx.post") as mock_post:
            mock_post.side_effect = httpx.HTTPError("network error")
            result = wire_arr_notifications(
                arr_url="http://sonarr:8989",
                arr_api_key="abc123",
                ntfy_url="http://ntfy:80",
                ntfy_topic="test",
            )
            assert result is False

    def test_wire_arr_notifications_returns_false_on_timeout(self):
        with patch("toolkit.services._arr.httpx.post") as mock_post:
            mock_post.side_effect = httpx.TimeoutException("timed out")
            result = wire_arr_notifications(
                arr_url="http://sonarr:8989",
                arr_api_key="abc123",
                ntfy_url="http://ntfy:80",
                ntfy_topic="test",
            )
            assert result is False


class TestWireProwlarrApps:
    def test_wire_prowlarr_apps_success(self):
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.post") as mock_post,
        ):
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = []
            mock_get.return_value = mock_get_resp

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 201
            mock_post.return_value = mock_post_resp

            logs = wire_prowlarr_apps(
                prowlarr_url="http://prowlarr:9696",
                prowlarr_api_key="prowlarr_key",
                sonarr_url="http://sonarr:8989",
                sonarr_api_key="sonarr_key",
                radarr_url="http://radarr:7878",
                radarr_api_key="radarr_key",
            )
            assert len(logs) == 2
            assert "registered Sonarr" in logs[0]
            assert "registered Radarr" in logs[1]

    def test_wire_prowlarr_apps_skips_existing(self):
        with patch("toolkit.services._arr.httpx.get") as mock_get:
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = [{"name": "Sonarr"}, {"name": "Radarr"}]
            mock_get.return_value = mock_get_resp

            logs = wire_prowlarr_apps(
                prowlarr_url="http://prowlarr:9696",
                prowlarr_api_key="prowlarr_key",
                sonarr_url="http://sonarr:8989",
                sonarr_api_key="sonarr_key",
                radarr_url="http://radarr:7878",
                radarr_api_key="radarr_key",
            )
            assert len(logs) == 2
            assert all("already registered" in log for log in logs)

    def test_wire_prowlarr_apps_app_exists_raises_httperror(self):
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.post") as mock_post,
        ):
            mock_get.side_effect = httpx.HTTPError("connection failed")

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 201
            mock_post.return_value = mock_post_resp

            logs = wire_prowlarr_apps(
                prowlarr_url="http://prowlarr:9696",
                prowlarr_api_key="prowlarr_key",
                sonarr_url="http://sonarr:8989",
                sonarr_api_key="sonarr_key",
                radarr_url="http://radarr:7878",
                radarr_api_key="radarr_key",
            )
            assert len(logs) == 2
            assert mock_post.call_count == 2

    def test_wire_prowlarr_apps_post_raises_httperror(self):
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.post") as mock_post,
        ):
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = []
            mock_get.return_value = mock_get_resp
            mock_post.side_effect = httpx.HTTPError("connection refused")

            logs = wire_prowlarr_apps(
                prowlarr_url="http://prowlarr:9696",
                prowlarr_api_key="prowlarr_key",
                sonarr_url="http://sonarr:8989",
                sonarr_api_key="sonarr_key",
                radarr_url="http://radarr:7878",
                radarr_api_key="radarr_key",
            )
            assert len(logs) == 2
            assert all("could not reach" in log for log in logs)
            assert "Sonarr" in logs[0]
            assert "Radarr" in logs[1]

    def test_wire_prowlarr_apps_mixed_results(self):
        with (
            patch("toolkit.services._arr.httpx.get") as mock_get,
            patch("toolkit.services._arr.httpx.post") as mock_post,
        ):
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = [{"name": "Sonarr"}]
            mock_get.return_value = mock_get_resp

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 201
            mock_post.return_value = mock_post_resp

            logs = wire_prowlarr_apps(
                prowlarr_url="http://prowlarr:9696",
                prowlarr_api_key="prowlarr_key",
                sonarr_url="http://sonarr:8989",
                sonarr_api_key="sonarr_key",
                radarr_url="http://radarr:7878",
                radarr_api_key="radarr_key",
            )
            assert "already registered" in logs[0]
            assert "registered Radarr" in logs[1]
