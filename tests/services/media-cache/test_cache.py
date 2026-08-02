"""Tests for the media-cache plugin client and integration helpers."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from toolkit.core.config.config import Config, ExternalHost, SSHConfig

cache_client_module = import_module("toolkit.services.media-cache.client")
CacheStats = cache_client_module.CacheStats
MediaCacheClient = cache_client_module.MediaCacheClient
build_external_media_cache_sftp_fields = cache_client_module.build_external_media_cache_sftp_fields
external_media_cache_remote_name = cache_client_module.external_media_cache_remote_name
render_external_media_cache_config = cache_client_module.render_external_media_cache_config
register_tautulli_webhook = cache_client_module.register_tautulli_webhook

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "homelab"


@pytest.fixture
def config_path(tmp_root: Path) -> Path:
    return tmp_root / "config.yaml"


@pytest.fixture
def sample_external_host() -> ExternalHost:
    return ExternalHost(
        name="nas-01",
        ip="10.10.10.100",
        ssh_user="root",
        ssh_port=22,
        services=["media-cache"],
        integrations={"media-cache": {"path": "/mnt/media"}},
    )


def make_config(
    tmp_root: Path,
    auth_method: str = "password",
    password: str = "secret123",
    key_file: str = "",
    external_hosts: list[ExternalHost] | None = None,
) -> None:
    """Write a config.yaml with given SSH auth settings."""
    cfg = Config(
        domain="example.com",
        email="admin@example.com",
        ssh=SSHConfig(
            auth_method=auth_method,
            password=password,
            key_file=key_file,
        ),
        external_hosts=external_hosts or [],
    )
    path = tmp_root / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(cfg.model_dump(mode="json", exclude_defaults=False)))


# ---------------------------------------------------------------------------
# MediaCacheClient
# ---------------------------------------------------------------------------


class TestMediaCacheClientStats:
    """Tests for MediaCacheClient.stats()."""

    @patch.object(cache_client_module.httpx, "get")
    def test_stats_returns_cache_stats(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tracked_files": 1234,
            "cache_used_bytes": 5_000_000_000,
            "cache_used_pct": 50.0,
            "active_prefetch": 3,
            "cold_after_days": 14,
            "bandwidth": {
                "effective_uplink_mbps": 100.5,
                "configured_uplink_mbps": 1000,
                "observed_uplink_mbps": 95.2,
                "time_to_first_frame_seconds": 0.5,
                "episode_fetch_seconds": 120,
                "movie_fetch_seconds": 300,
                "max_concurrent_4k": 4,
                "max_concurrent_1080p": 10,
                "source": "observed",
                "observed_samples": 50,
            },
        }
        mock_get.return_value = mock_resp

        client = MediaCacheClient()
        stats = client.stats()

        assert stats.total_files == 1234
        assert stats.cache_size_gb == round(5_000_000_000 / (1024**3), 2)
        assert stats.cache_used_pct == 50.0
        assert stats.active_prefetch == 3
        assert stats.cold_after_days == 14
        assert stats.effective_uplink_mbps == 100.5
        assert stats.configured_uplink_mbps == 1000
        assert stats.observed_uplink_mbps == 95.2
        assert stats.time_to_first_frame_seconds == 0.5
        assert stats.episode_fetch_seconds == 120
        assert stats.movie_fetch_seconds == 300
        assert stats.max_concurrent_4k == 4
        assert stats.max_concurrent_1080p == 10
        assert stats.bandwidth_source == "observed"
        assert stats.observed_samples == 50

    @patch.object(cache_client_module.httpx, "get")
    def test_stats_returns_defaults_on_http_error(self, mock_get: MagicMock) -> None:
        import httpx

        mock_get.side_effect = httpx.HTTPError("connection refused")

        client = MediaCacheClient()
        stats = client.stats()

        assert stats == CacheStats()

    @patch.object(cache_client_module.httpx, "get")
    def test_stats_handles_missing_bandwidth_dict(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tracked_files": 0,
            "cache_used_bytes": 0,
            "bandwidth": "not-a-dict",  # API may return non-dict
        }
        mock_get.return_value = mock_resp

        client = MediaCacheClient()
        stats = client.stats()

        # Should not raise, defaults should apply
        assert stats.total_files == 0
        assert stats.cache_size_gb == 0.0
        assert stats.effective_uplink_mbps == 0.0


class TestMediaCacheClientHealth:
    """Tests for MediaCacheClient.health()."""

    @patch.object(cache_client_module.httpx, "get")
    def test_health_returns_true_on_200(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock(status_code=200)
        mock_get.return_value = mock_resp

        client = MediaCacheClient()
        assert client.health() is True

    @patch.object(cache_client_module.httpx, "get")
    def test_health_returns_false_on_non_200(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock(status_code=503)
        mock_get.return_value = mock_resp

        client = MediaCacheClient()
        assert client.health() is False

    @patch.object(cache_client_module.httpx, "get")
    def test_health_returns_false_on_http_error(self, mock_get: MagicMock) -> None:
        import httpx

        mock_get.side_effect = httpx.HTTPError("connection refused")

        client = MediaCacheClient()
        assert client.health() is False


class TestMediaCacheClientBackends:
    """Tests for MediaCacheClient.backends()."""

    @patch.object(cache_client_module.httpx, "get")
    def test_backends_returns_list(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"backends": ["nas", "b2", "gdrive"]}
        mock_get.return_value = mock_resp

        client = MediaCacheClient()
        result = client.backends()

        assert result == [
            {"name": "nas", "path": "nas:"},
            {"name": "b2", "path": "b2:"},
            {"name": "gdrive", "path": "gdrive:"},
        ]

    @patch.object(cache_client_module.httpx, "get")
    def test_backends_returns_empty_on_http_error(self, mock_get: MagicMock) -> None:
        import httpx

        mock_get.side_effect = httpx.HTTPError("connection refused")

        client = MediaCacheClient()
        result = client.backends()

        assert result == []


class TestMediaCacheClientWebhookUrl:
    """Tests for MediaCacheClient.webhook_url()."""

    def test_webhook_url_jellyfin(self) -> None:
        client = MediaCacheClient()
        assert client.webhook_url("jellyfin") == "http://media-cache:8686/webhook/jellyfin"

    def test_webhook_url_plex(self) -> None:
        client = MediaCacheClient(base_url="http://cache.example.com:9000")
        assert client.webhook_url("plex") == "http://cache.example.com:9000/webhook/plex"

    def test_webhook_url_strips_trailing_slash(self) -> None:
        client = MediaCacheClient(base_url="http://cache.example.com:9000/")
        assert client.webhook_url("tautulli") == "http://cache.example.com:9000/webhook/tautulli"


# ---------------------------------------------------------------------------
# external_media_cache_remote_name
# ---------------------------------------------------------------------------


class TestExternalMediaCacheRemoteName:
    """Tests for external_media_cache_remote_name()."""

    def test_basic_name(self) -> None:
        host = ExternalHost(name="nas-01", ip="10.0.0.1")
        assert external_media_cache_remote_name(host) == "ext-nas-01"

    def test_name_with_spaces(self) -> None:
        host = ExternalHost.model_construct(name="My NAS Server", ip="10.0.0.1")
        assert external_media_cache_remote_name(host) == "ext-My-NAS-Server"

    def test_name_with_special_chars(self) -> None:
        host = ExternalHost.model_construct(name="server@home!", ip="10.0.0.1")
        assert external_media_cache_remote_name(host) == "ext-server-home"

    def test_empty_name_defaults_to_host(self) -> None:
        host = ExternalHost.model_construct(name="", ip="10.0.0.1")
        assert external_media_cache_remote_name(host) == "ext-host"


# ---------------------------------------------------------------------------
# build_external_media_cache_sftp_fields
# ---------------------------------------------------------------------------


class TestBuildExternalMediaCacheSftpFields:
    """Tests for validated service-owned SFTP projection fields."""

    def test_password_auth_happy_path(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Password auth: params include host, user, port, path, pass."""
        make_config(tmp_root, auth_method="password", password="ssh-secret")

        params = build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

        assert params["host"] == "10.10.10.100"
        assert params["user"] == "root"
        assert params["port"] == "22"
        assert params["path"] == "/mnt/media"
        assert params["pass"] == "ssh-secret"

    def test_password_auth_missing_password_raises(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Password auth with no password set raises ValueError."""
        make_config(tmp_root, auth_method="password", password="")

        with pytest.raises(ValueError, match="Global SSH password is required"):
            build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

    def test_key_auth_missing_key_file_raises(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Key auth with no key_file set raises ValueError."""
        make_config(tmp_root, auth_method="key", password="", key_file="")

        with pytest.raises(ValueError, match="Global SSH key file is required"):
            build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

    def test_key_auth_missing_key_file_on_disk_raises(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Key auth with key_file path that doesn't exist raises ValueError."""
        make_config(tmp_root, auth_method="key", password="", key_file="/nonexistent/id_rsa")

        with pytest.raises(ValueError, match="SSH key file not found"):
            build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

    def test_key_auth_happy_path(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Key auth: key file is copied to rclone/keys/ with correct permissions."""
        # Create a real key file
        key_source = tmp_root / "my_key.pem"
        key_source.parent.mkdir(parents=True, exist_ok=True)
        key_source.write_text("test key material\n")

        make_config(tmp_root, auth_method="key", password="", key_file=str(key_source))

        params = build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

        assert "pass" not in params
        assert "key_file" in params
        # key_file should be an absolute path under /config/rclone/keys/
        assert params["key_file"].startswith("/config/rclone/keys/")
        assert params["key_file"].endswith(".pem")

        # The key should be copied to the keys directory
        keys_dir = tmp_root / "config" / "rclone" / "keys"
        assert keys_dir.exists()
        copied_keys = list(keys_dir.glob("*.pem"))
        assert len(copied_keys) == 1
        assert copied_keys[0].read_text() == "test key material\n"
        # Verify permissions are restrictive
        assert (copied_keys[0].stat().st_mode & 0o777) == 0o600

    def test_key_auth_appends_newline_if_missing(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """Key file content without trailing newline gets one appended."""
        key_source = tmp_root / "my_key.pem"
        key_source.parent.mkdir(parents=True, exist_ok=True)
        key_source.write_text("test key material")  # no trailing newline

        make_config(tmp_root, auth_method="key", password="", key_file=str(key_source))

        build_external_media_cache_sftp_fields(tmp_root, sample_external_host)

        keys_dir = tmp_root / "config" / "rclone" / "keys"
        copied_key = list(keys_dir.glob("*.pem"))[0]
        assert copied_key.read_text() == "test key material\n"

    def test_path_stripping(self, tmp_root: Path, sample_external_host: ExternalHost) -> None:
        """The manifest-owned media path is stripped of surrounding whitespace."""
        host = ExternalHost.model_construct(
            name="nas-01",
            ip="10.10.10.100",
            ssh_user="root",
            ssh_port=22,
            services=["media-cache"],
            integrations={"media-cache": {"path": "  /mnt/media  "}},
        )
        make_config(tmp_root, auth_method="password", password="secret")

        params = build_external_media_cache_sftp_fields(tmp_root, host)

        assert params["path"] == "/mnt/media"


# ---------------------------------------------------------------------------
# render_external_media_cache_config
# ---------------------------------------------------------------------------


class TestRenderExternalMediaCacheConfig:
    """The controller projects all remotes into one private rclone file."""

    def test_password_auth_writes_obscured_rclone_config(
        self,
        tmp_root: Path,
        sample_external_host: ExternalHost,
        monkeypatch,
    ) -> None:
        make_config(tmp_root, auth_method="password", password="secret", external_hosts=[sample_external_host])
        monkeypatch.setattr(cache_client_module.shutil, "which", lambda _name: "/usr/bin/rclone")
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def obscure(*args: object, **kwargs: object) -> MagicMock:
            calls.append((args, kwargs))
            return MagicMock(returncode=0, stdout="obscured-password\n")

        monkeypatch.setattr(cache_client_module.subprocess, "run", obscure)

        path, count = render_external_media_cache_config(tmp_root)

        assert count == 1
        assert path == tmp_root / "config" / "rclone" / "rclone.conf"
        assert path.stat().st_mode & 0o777 == 0o600
        content = path.read_text()
        assert "[ext-nas-01]" in content
        assert "host = 10.10.10.100" in content
        assert "pass = obscured-password" in content
        assert "[media-union]" in content
        assert "upstreams = ext-nas-01:/mnt/media" in content
        assert "secret" not in content
        assert calls[0][0][0][-1] == "-"
        assert calls[0][1]["input"] == "secret\n"

    def test_key_auth_copies_private_key_and_references_it(
        self,
        tmp_root: Path,
        sample_external_host: ExternalHost,
    ) -> None:
        key_source = tmp_root / "id_ed25519"
        key_source.parent.mkdir(parents=True, exist_ok=True)
        key_source.write_text("private-key\n", encoding="utf-8")
        make_config(
            tmp_root,
            auth_method="key",
            key_file=str(key_source),
            external_hosts=[sample_external_host],
        )

        path, count = render_external_media_cache_config(tmp_root)

        copied_key = tmp_root / "config" / "rclone" / "keys" / "ext-nas-01.pem"
        assert count == 1
        assert "key_file = /config/rclone/keys/ext-nas-01.pem" in path.read_text()
        assert copied_key.read_text() == "private-key\n"
        assert copied_key.stat().st_mode & 0o777 == 0o600

    def test_union_quotes_paths_with_spaces_and_quotes(
        self,
        tmp_root: Path,
        sample_external_host: ExternalHost,
        monkeypatch,
    ) -> None:
        host = sample_external_host.model_copy(update={"integrations": {"media-cache": {"path": '/srv/My "Media"'}}})
        make_config(tmp_root, auth_method="password", password="secret", external_hosts=[host])
        monkeypatch.setattr(cache_client_module.shutil, "which", lambda _name: "/usr/bin/rclone")
        monkeypatch.setattr(
            cache_client_module.subprocess,
            "run",
            lambda *_args, **_kwargs: MagicMock(returncode=0, stdout="obscured\n"),
        )

        path, _count = render_external_media_cache_config(tmp_root)

        assert 'upstreams = "ext-nas-01:/srv/My \\"Media\\""' in path.read_text()

    def test_union_rejects_colliding_generated_remote_names(self, tmp_root: Path, monkeypatch) -> None:
        first = ExternalHost.model_construct(
            name="nas one",
            ip="10.10.10.100",
            ssh_user="root",
            ssh_port=22,
            services=["media-cache"],
            integrations={"media-cache": {"path": "/srv/one"}},
        )
        second = ExternalHost.model_construct(
            name="nas-one",
            ip="10.10.10.101",
            ssh_user="root",
            ssh_port=22,
            services=["media-cache"],
            integrations={"media-cache": {"path": "/srv/two"}},
        )
        make_config(tmp_root, auth_method="password", password="secret", external_hosts=[])
        monkeypatch.setattr(
            cache_client_module, "_external_media_cache_hosts", lambda *_args, **_kwargs: [first, second]
        )
        monkeypatch.setattr(cache_client_module.shutil, "which", lambda _name: "/usr/bin/rclone")
        monkeypatch.setattr(
            cache_client_module.subprocess,
            "run",
            lambda *_args, **_kwargs: MagicMock(returncode=0, stdout="obscured\n"),
        )

        with pytest.raises(ValueError, match="generate the same rclone remote"):
            render_external_media_cache_config(tmp_root)

    def test_empty_pool_removes_config_and_stale_keys(
        self,
        tmp_root: Path,
        sample_external_host: ExternalHost,
    ) -> None:
        key_source = tmp_root / "id_ed25519"
        key_source.parent.mkdir(parents=True, exist_ok=True)
        key_source.write_text("private-key\n", encoding="utf-8")
        make_config(tmp_root, auth_method="key", key_file=str(key_source), external_hosts=[sample_external_host])
        render_external_media_cache_config(tmp_root)
        config_file = tmp_root / "config" / "rclone" / "rclone.conf"
        stale_key = tmp_root / "config" / "rclone" / "keys" / "ext-nas-01.pem"
        assert config_file.exists() and stale_key.exists()

        make_config(tmp_root, auth_method="key", key_file=str(key_source), external_hosts=[])
        path, count = render_external_media_cache_config(tmp_root)

        assert count == 0
        assert path == config_file
        assert not config_file.exists()
        assert not stale_key.exists()


# ---------------------------------------------------------------------------
# register_tautulli_webhook
# ---------------------------------------------------------------------------


def _tautulli_response(result: str, data: object = None) -> MagicMock:
    """Build a mock httpx response shaped like Tautulli's API envelope."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"response": {"result": result, "data": data}}
    return resp


class TestRegisterTautulliWebhook:
    """Tests for register_tautulli_webhook()."""

    WEBHOOK_URL = "http://media-cache:8686/webhook/tautulli"

    @patch.object(cache_client_module.httpx, "get")
    def test_skips_when_webhook_already_registered(self, mock_get: MagicMock) -> None:
        """If a webhook notifier already points at our URL, return ok without creating a new one."""
        notifiers = [{"id": 7, "agent_id": 25, "agent_name": "webhook", "friendly_name": "media-cache"}]
        notifier_cfg = {
            "id": 7,
            "agent_id": 25,
            "friendly_name": "media-cache",
            "config": {"hook": TestRegisterTautulliWebhook.WEBHOOK_URL, "method": "POST"},
        }
        mock_get.side_effect = [
            _tautulli_response("success", notifiers),  # get_notifiers
            _tautulli_response("success", notifier_cfg),  # get_notifier_config
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
        )

        assert ok is True
        assert "already registered" in message
        # Only the two GETs — no add_notifier_config, no set_notifier_config.
        assert mock_get.call_count == 2

    @patch.object(cache_client_module.httpx, "get")
    def test_skips_when_authenticated_webhook_headers_match(self, mock_get: MagicMock) -> None:
        expected_headers = '{"X-Media-Cache-Webhook-Token":"webhook-secret"}'
        notifiers = [{"id": 7, "agent_id": 25, "agent_name": "webhook", "friendly_name": "media-cache"}]
        notifier_cfg = {
            "id": 7,
            "agent_id": 25,
            "friendly_name": "media-cache",
            "config": {"hook": self.WEBHOOK_URL, "method": "POST"},
            "notify_text": {
                "on_play": {"subject": expected_headers},
                "on_resume": {"subject": expected_headers},
            },
        }
        mock_get.side_effect = [
            _tautulli_response("success", notifiers),
            _tautulli_response("success", notifier_cfg),
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=self.WEBHOOK_URL,
            webhook_token="webhook-secret",
        )

        assert ok is True
        assert "already registered" in message
        assert mock_get.call_count == 2

    @patch.object(cache_client_module.httpx, "get")
    def test_reconfigures_stale_authenticated_webhook_headers(self, mock_get: MagicMock) -> None:
        notifiers = [{"id": 7, "agent_id": 25, "agent_name": "webhook", "friendly_name": "media-cache"}]
        notifier_cfg = {
            "id": 7,
            "agent_id": 25,
            "friendly_name": "media-cache",
            "config": {"hook": self.WEBHOOK_URL, "method": "POST"},
            "notify_text": {
                "on_play": {"subject": '{"X-Media-Cache-Webhook-Token":"old"}'},
                "on_resume": {"subject": '{"X-Media-Cache-Webhook-Token":"old"}'},
            },
        }
        mock_get.side_effect = [
            _tautulli_response("success", notifiers),
            _tautulli_response("success", notifier_cfg),
            _tautulli_response("success", None),
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=self.WEBHOOK_URL,
            webhook_token="webhook-secret",
        )

        assert ok is True
        assert "authentication updated" in message
        assert mock_get.call_args.kwargs["params"]["on_play_subject"].endswith('"webhook-secret"}')

    @patch.object(cache_client_module.httpx, "get")
    def test_creates_and_configures_new_notifier(self, mock_get: MagicMock) -> None:
        """When no existing webhook matches, create a new notifier then configure it."""
        empty_notifiers: list = []
        after_add_notifiers = [{"id": 12, "agent_id": 25, "agent_name": "webhook", "friendly_name": ""}]
        mock_get.side_effect = [
            _tautulli_response("success", empty_notifiers),  # initial get_notifiers
            _tautulli_response("success", None),  # add_notifier_config (returns null data)
            _tautulli_response("success", after_add_notifiers),  # get_notifiers after add
            _tautulli_response("success", None),  # set_notifier_config
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
            webhook_token="webhook-secret",
        )

        assert ok is True
        assert "registered" in message
        assert "12" in message
        # Verify set_notifier_config was the last call with the right params.
        set_call = mock_get.call_args_list[3]
        assert set_call.kwargs["params"]["cmd"] == "set_notifier_config"
        assert set_call.kwargs["params"]["notifier_id"] == 12
        assert set_call.kwargs["params"]["webhook_hook"] == TestRegisterTautulliWebhook.WEBHOOK_URL
        assert set_call.kwargs["params"]["webhook_method"] == "POST"
        expected_headers = '{"X-Media-Cache-Webhook-Token":"webhook-secret"}'
        assert set_call.kwargs["params"]["on_play_subject"] == expected_headers
        assert set_call.kwargs["params"]["on_resume_subject"] == expected_headers
        assert set_call.kwargs["params"]["on_play"] == 1
        assert set_call.kwargs["params"]["on_resume"] == 1
        assert "{action}" in set_call.kwargs["params"]["on_play_body"]
        assert "{media_type}" in set_call.kwargs["params"]["on_play_body"]

    @patch.object(cache_client_module.httpx, "get")
    def test_reconfigures_existing_friendly_name_slot(self, mock_get: MagicMock) -> None:
        """A webhook notifier with our friendly_name but a stale URL gets reconfigured."""
        notifiers = [{"id": 5, "agent_id": 25, "agent_name": "webhook", "friendly_name": "media-cache"}]
        stale_cfg = {
            "id": 5,
            "agent_id": 25,
            "friendly_name": "media-cache",
            "config": {"hook": "http://old.example.com/hook", "method": "POST"},
        }
        mock_get.side_effect = [
            _tautulli_response("success", notifiers),  # get_notifiers
            _tautulli_response("success", stale_cfg),  # get_notifier_config
            _tautulli_response("success", None),  # set_notifier_config
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
        )

        assert ok is True
        assert "reconfigured" in message
        assert "5" in message

    @patch.object(cache_client_module.httpx, "get")
    def test_returns_false_when_add_notifier_fails(self, mock_get: MagicMock) -> None:
        """If add_notifier_config reports failure, return False with a clear message."""
        mock_get.side_effect = [
            _tautulli_response("success", []),  # get_notifiers
            _tautulli_response("error", None),  # add_notifier_config fails
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
        )

        assert ok is False
        assert "add_notifier_config failed" in message

    @patch.object(cache_client_module.httpx, "get")
    def test_returns_false_on_http_error(self, mock_get: MagicMock) -> None:
        """Network errors surface as a failure result, never an exception."""
        mock_get.side_effect = httpx.HTTPError("tautulli unreachable")

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
        )

        assert ok is False
        assert "add_notifier_config failed" in message

    @patch.object(cache_client_module.httpx, "get")
    def test_skips_non_webhook_notifiers(self, mock_get: MagicMock) -> None:
        """Notifiers with a different agent_id (e.g. Telegram) are ignored."""
        notifiers = [
            {"id": 1, "agent_id": 13, "agent_name": "telegram", "friendly_name": "my bot"},
        ]
        after_add_notifiers = [
            {"id": 1, "agent_id": 13, "agent_name": "telegram", "friendly_name": "my bot"},
            {"id": 2, "agent_id": 25, "agent_name": "webhook", "friendly_name": ""},
        ]
        mock_get.side_effect = [
            _tautulli_response("success", notifiers),  # get_notifiers
            _tautulli_response("success", None),  # add_notifier_config
            _tautulli_response("success", after_add_notifiers),  # get_notifiers after add
            _tautulli_response("success", None),  # set_notifier_config
        ]

        ok, message = register_tautulli_webhook(
            tautulli_url="http://tautulli:8181",
            api_key="key",
            webhook_url=TestRegisterTautulliWebhook.WEBHOOK_URL,
        )

        assert ok is True
        assert "2" in message
