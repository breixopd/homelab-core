from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from toolkit.controller.transport_auth import load_or_create_transport_token, read_transport_token


def test_transport_token_is_created_owner_only_and_reused(tmp_path: Path) -> None:
    token_path = tmp_path / "private" / "controller.token"

    first = load_or_create_transport_token(token_path)
    second = load_or_create_transport_token(token_path)

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert read_transport_token(token_path) == first


def test_transport_token_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x" * 48)
    target.chmod(0o600)
    token_path = tmp_path / "controller.token"
    token_path.symlink_to(target)

    with pytest.raises(OSError):
        load_or_create_transport_token(token_path)

    assert target.read_text() == "x" * 48


def test_transport_token_rejects_group_readable_file(tmp_path: Path) -> None:
    token_path = tmp_path / "controller.token"
    token_path.write_text("x" * 48)
    token_path.chmod(0o640)

    with pytest.raises(RuntimeError, match="owner-only"):
        read_transport_token(token_path)


def test_transport_token_reader_rejects_relative_paths() -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        read_transport_token(Path("relative/controller.token"))


def test_transport_token_write_failure_never_publishes_partial_final_path(tmp_path: Path, monkeypatch) -> None:
    token_path = tmp_path / "controller.token"

    def fail_write(_descriptor: int, _content) -> int:
        raise OSError("simulated interrupted write")

    monkeypatch.setattr("toolkit.controller.transport_auth.os.write", fail_write)

    with pytest.raises(OSError, match="interrupted"):
        load_or_create_transport_token(token_path)

    assert not token_path.exists()


def test_group_scoped_ui_token_is_not_world_readable(tmp_path: Path) -> None:
    token_path = tmp_path / "ui.token"

    previous_umask = os.umask(0o077)
    try:
        token = load_or_create_transport_token(token_path, group_gid=os.getgid())
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o640
    assert token_path.stat().st_gid == os.getgid()
    assert read_transport_token(token_path, allow_group_read=True) == token
    with pytest.raises(RuntimeError, match="owner-only"):
        read_transport_token(token_path)


def test_existing_ui_token_permissions_are_reconciled_without_rotation(tmp_path: Path) -> None:
    token_path = tmp_path / "ui.token"
    original = load_or_create_transport_token(token_path)

    reconciled = load_or_create_transport_token(token_path, group_gid=os.getgid())

    assert reconciled == original
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o640
