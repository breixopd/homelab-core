from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from toolkit.services.ntfy.client import NtfyClient


def _mock_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


class TestNtfyClient:
    def test_send(self):
        with patch("httpx.post", return_value=_mock_response(200)):
            assert NtfyClient().send("alerts", "test message", title="Test") is True

    def test_send_fail(self):
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            assert NtfyClient().send("alerts", "test") is False

    def test_health(self):
        with patch("httpx.get", return_value=_mock_response(200)):
            assert NtfyClient().health() is True
