from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services.tdarr.bootstrap import ensure_tdarr_plugins


@patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=True)
@patch("toolkit.services.tdarr.bootstrap.httpx.post")
def test_ensure_tdarr_plugins_triggers_update(mock_post, _mock_wait):
    mock_post.return_value = MagicMock(status_code=200)
    logs = ensure_tdarr_plugins("http://tdarr:8265", timeout=30)
    mock_post.assert_any_call(
        "http://tdarr:8265/api/v2/update-plugins",
        json={"data": {"force": True}},
        timeout=30,
    )
    assert any("plugin update" in line.lower() for line in logs)


def test_tdarr_post_start_owns_library_and_flow_setup(tmp_path):
    cfg = Config(
        domain="test.local",
        services=ServicesConfig(management=True, media=True),
        service_settings={"tdarr": {"enabled": True}},
    )
    with patch(
        "toolkit.services.tdarr.bootstrap.configure_tdarr",
        return_value=["Tdarr: libraries ready"],
    ) as configure:
        logs = load_plugin("tdarr").TdarrPlugin().post_start(cfg, {}, root=tmp_path)

    assert logs == ["Tdarr: libraries ready"]
    configure.assert_called_once_with(cfg, root=tmp_path)


def test_tdarr_post_start_degrades_to_warning(tmp_path):
    cfg = Config(
        domain="test.local",
        services=ServicesConfig(management=True, media=True),
        service_settings={"tdarr": {"enabled": True}},
    )
    with patch("toolkit.services.tdarr.bootstrap.configure_tdarr", side_effect=RuntimeError("warming up")):
        logs = load_plugin("tdarr").TdarrPlugin().post_start(cfg, {}, root=tmp_path)

    assert logs == ["WARNING: Tdarr setup failed: warming up"]
