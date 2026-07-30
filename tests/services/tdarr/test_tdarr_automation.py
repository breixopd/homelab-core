from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from toolkit.core.config.config import Config
from toolkit.services.tdarr.bootstrap import _cruddb, ensure_tdarr_libraries, ensure_tdarr_plugins, wait_for_tdarr


def test_tdarr_healthcheck_uses_documented_server_status_port() -> None:
    compose = yaml.safe_load(
        (Path(__file__).parents[3] / "toolkit/services/tdarr/compose.yaml").read_text(encoding="utf-8")
    )

    command = compose["services"]["tdarr"]["healthcheck"]["test"][1]
    assert "127.0.0.1:8266/api/v2/status" in command


def test_wait_for_tdarr_success():
    with patch("toolkit.services.tdarr.bootstrap.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        assert wait_for_tdarr("http://tdarr:8265", timeout=5) is True


@patch("toolkit.services.tdarr.bootstrap.httpx.post")
def test_cruddb_accepts_successful_empty_responses(post) -> None:
    response = MagicMock(status_code=200, content=b"")
    post.return_value = response

    assert _cruddb("http://bridge:8265", {"collection": "FlowsJSONDB", "mode": "getAll"}) == []
    assert _cruddb("http://bridge:8265", {"collection": "FlowsJSONDB", "mode": "insert"}) == {}
    assert response.raise_for_status.call_count == 2


@patch("toolkit.services.tdarr.bootstrap.httpx.post")
@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=True)
def test_tdarr_plugin_refresh_skips_when_templates_exist(_wait, post) -> None:
    post.return_value = MagicMock(
        status_code=200,
        json=lambda: [[{"name": "Run Health Check"}], "Community"],
    )
    logs = ensure_tdarr_plugins("http://bridge:8265")

    post.assert_called_once()
    assert post.call_args.args[0].endswith("/api/v2/search-flow-templates")
    assert logs == ["Tdarr: 1 community flow template(s) available"]


@patch("toolkit.services.tdarr.bootstrap.time.sleep")
@patch("toolkit.services.tdarr.bootstrap.httpx.post")
@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=True)
def test_tdarr_plugin_refresh_does_not_retry_failed_endpoint(_wait, post, sleep) -> None:
    empty = MagicMock(status_code=200, json=lambda: [[], "Community"])
    failed = MagicMock(status_code=400)
    post.side_effect = [empty, failed]
    logs = ensure_tdarr_plugins("http://bridge:8265")

    assert post.call_count == 2
    assert post.call_args.kwargs["json"] == {"data": {"force": True}}
    sleep.assert_not_called()
    assert "Tdarr: update-plugins HTTP 400" in logs


@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=True)
@patch("toolkit.services.tdarr.bootstrap._existing_library_folders", return_value=set())
@patch("toolkit.services.tdarr.bootstrap._cruddb")
def test_ensure_tdarr_libraries_creates_two(mock_cruddb, _mock_folders, _mock_wait):
    logs = ensure_tdarr_libraries("http://tdarr:8265")
    assert mock_cruddb.call_count == 2
    assert any("Movies" in line for line in logs)
    assert any("TV" in line for line in logs)


@patch("toolkit.services.tdarr.bootstrap._cruddb", return_value=[])
@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=False)
@patch("toolkit.services.tdarr.bootstrap.ensure_tdarr_flow", return_value=[])
@patch(
    "toolkit.services.tdarr.bootstrap.ensure_tdarr_libraries",
    return_value=["Tdarr: created library Movies"],
)
def test_configure_tdarr_defers_when_api_is_unavailable(_mock_libs, _mock_flow, _mock_wait, _mock_cruddb):
    from toolkit.services.tdarr.bootstrap import configure_tdarr

    cfg = Config(domain="example.com")
    logs = configure_tdarr(cfg, install_root="/opt/homelab")

    assert "Tdarr: API not ready — bootstrap deferred" in logs
    _mock_cruddb.assert_not_called()
    _mock_flow.assert_not_called()
    _mock_libs.assert_not_called()


@patch("toolkit.services.tdarr.bootstrap._cruddb", return_value=[])
@patch("toolkit.services.tdarr.bootstrap.ensure_tdarr_libraries", return_value=[])
@patch("toolkit.services.tdarr.bootstrap.ensure_tdarr_flow", return_value=[])
@patch("toolkit.services.tdarr.bootstrap.ensure_tdarr_plugins", return_value=[])
@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=True)
@patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://172.31.250.140:8265")
def test_configure_tdarr_uses_host_reachable_container_url(
    _resolve_url,
    _wait,
    plugins,
    flow,
    libraries,
    cruddb,
) -> None:
    from toolkit.services.tdarr.bootstrap import configure_tdarr

    configure_tdarr(Config(domain="example.com"), install_root="/opt/homelab")

    plugins.assert_called_once_with("http://172.31.250.140:8265", ready=True)
    flow.assert_called_once_with("http://172.31.250.140:8265", root=Path("/opt/homelab"), ready=True)
    libraries.assert_called_once()
    assert libraries.call_args.args[0] == "http://172.31.250.140:8265"
    assert libraries.call_args.kwargs["ready"] is True
    assert cruddb.call_args.args[0] == "http://172.31.250.140:8265"
