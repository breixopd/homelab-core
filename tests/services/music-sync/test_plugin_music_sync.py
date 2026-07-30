from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config


def test_music_sync_action_uses_secure_request_transport(tmp_path: Path) -> None:
    plugin = load_plugin("music-sync").MusicSyncPlugin()
    with (
        patch("toolkit.services.sdk.docker_curl", return_value=(0, "accepted")) as request,
        patch(
            "toolkit.services.sdk.docker_exec_on_vm",
            side_effect=AssertionError("raw command transport is forbidden"),
        ),
    ):
        logs = plugin.execute_action(
            "sync-now",
            Config(),
            {"MUSIC_SYNC_WEB_USERNAME": "operator", "MUSIC_SYNC_WEB_PASSWORD": "test-only-secret"},
            tmp_path,
        )

    assert logs == ["Music synchronization accepted"]
    assert request.call_args.args[1:4] == (
        plugin.runtime_address(Config()),
        "music-sync",
        "http://localhost:8845/api/sync",
    )
    assert request.call_args.kwargs["method"] == "POST"
    auth = request.call_args.kwargs["headers"]["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth.removeprefix("Basic ")).decode() == "operator:test-only-secret"
