from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from toolkit.core.ops.updates import UpdateCheckError, run_check, run_framework_check


def test_image_update_scan_rejects_failed_process(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess([], 1, stdout="", stderr="registry unavailable")
    with (
        patch("toolkit.core.ops.updates.subprocess.run", return_value=completed),
        pytest.raises(UpdateCheckError, match="image update scan failed: registry unavailable"),
    ):
        run_check(tmp_path)


def test_image_update_scan_rejects_malformed_report(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess([], 0, stdout='{"status":"ok"}', stderr="")
    with (
        patch("toolkit.core.ops.updates.subprocess.run", return_value=completed),
        pytest.raises(UpdateCheckError, match="invalid report"),
    ):
        run_check(tmp_path)


def test_image_update_scan_accepts_empty_successful_report(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    with patch("toolkit.core.ops.updates.subprocess.run", return_value=completed):
        assert run_check(tmp_path) == []


def test_framework_update_scan_reports_timeout(tmp_path: Path) -> None:
    with (
        patch("toolkit.core.ops.updates.subprocess.run", side_effect=subprocess.TimeoutExpired([], 120)),
        pytest.raises(UpdateCheckError, match="framework update scan timed out"),
    ):
        run_framework_check(tmp_path)


def test_framework_update_scan_allows_resolver_and_registry_budget(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    with patch("toolkit.core.ops.updates.subprocess.run", return_value=completed) as run:
        assert run_framework_check(tmp_path) == []

    assert run.call_args.kwargs["timeout"] == 300
