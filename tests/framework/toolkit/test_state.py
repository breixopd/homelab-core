from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import ToolkitState, get_state


def test_uninitialized(tmp_path: Path):
    assert get_state(tmp_path) == ToolkitState.UNINITIALIZED


def test_config_only(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("domain: example.com\n")
    assert get_state(tmp_path) == ToolkitState.CONFIG_ONLY


def test_ready(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("domain: example.com\n")
    env_dir = tmp_path / "generated" / "infra"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text("BASE_DOMAIN=example.com\n")
    assert get_state(tmp_path) == ToolkitState.READY
