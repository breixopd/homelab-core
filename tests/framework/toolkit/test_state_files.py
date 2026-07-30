from __future__ import annotations

import json
from pathlib import Path

import pytest
from toolkit.core.state.files import atomic_write_json, atomic_write_text


def test_atomic_json_write_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "state" / "status.json"

    atomic_write_json(path, {"ok": True, "nodes": ["infra"]})

    assert json.loads(path.read_text()) == {"ok": True, "nodes": ["infra"]}


def test_atomic_json_write_preserves_previous_document_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "status.json"
    path.write_text('{"ok": true}\n')
    monkeypatch.setattr("toolkit.core.state.files.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("fail")))

    with pytest.raises(OSError):
        atomic_write_json(path, {"ok": False})

    assert json.loads(path.read_text()) == {"ok": True}


def test_atomic_text_write_replaces_existing_file_with_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "receipt.txt"

    atomic_write_text(path, "first\n")
    atomic_write_text(path, "second\n")

    assert path.read_text() == "second\n"
    assert path.stat().st_mode & 0o777 == 0o600
