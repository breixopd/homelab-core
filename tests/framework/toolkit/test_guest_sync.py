from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from toolkit.core.config.config import Config
from toolkit.core.deploy import guest_sync


def test_sync_guest_via_tar_delegates(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    from toolkit.core.config.config import save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="example.com"), config_path(root))

    with patch("toolkit.core.deploy.guest_sync.sync_repo_to_guest") as mock_sync:
        guest_sync.sync_guest_via_tar(root, "10.0.0.9", repo_dest="/srv/homelab")

    mock_sync.assert_called_once()
    _root, cfg, ip = mock_sync.call_args[0]
    assert ip == "10.0.0.9"
    assert cfg.domain == "example.com"
    assert mock_sync.call_args[1]["repo_dest"] == "/srv/homelab"


def test_main_success(tmp_path: Path):
    with patch("toolkit.core.deploy.guest_sync.sync_guest_via_tar"):
        rc = guest_sync.main(["--root", str(tmp_path), "--vm-ip", "10.0.0.1"])
    assert rc == 0


def test_main_failure_prints_stderr(tmp_path: Path, capsys):
    with patch(
        "toolkit.core.deploy.guest_sync.sync_guest_via_tar",
        side_effect=RuntimeError("scp failed"),
    ):
        rc = guest_sync.main(["--root", str(tmp_path), "--vm-ip", "10.0.0.1"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "scp failed" in captured.err
