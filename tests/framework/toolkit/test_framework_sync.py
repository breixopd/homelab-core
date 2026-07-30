from __future__ import annotations

import json
from pathlib import Path

import pytest
from toolkit.core.bootstrap.framework_sync import FrameworkSyncError, sync_framework


def _snapshot(root: Path, version: str, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".homelab-framework-version").write_text(f"{version}\n", encoding="utf-8")
    (root / ".homelab-framework-files.json").write_text(
        json.dumps(sorted(files)) + "\n",
        encoding="utf-8",
    )
    return root


def test_sync_framework_replaces_only_managed_files(tmp_path: Path) -> None:
    target = _snapshot(
        tmp_path / "target",
        "1.0.0",
        {
            "toolkit/old.py": "old\n",
            "toolkit/removed.py": "remove me\n",
            "scripts/install.sh": "old installer\n",
        },
    )
    (target / "config.yaml").write_text("domain: local.example\n", encoding="utf-8")
    (target / "data" / "state.db").parent.mkdir()
    (target / "data" / "state.db").write_text("state", encoding="utf-8")
    source = _snapshot(
        tmp_path / "source",
        "1.1.0",
        {
            "toolkit/old.py": "new\n",
            "toolkit/added.py": "added\n",
            "scripts/install.sh": "new installer\n",
        },
    )

    result = sync_framework(source, target)

    assert result.previous_version == "1.0.0"
    assert result.version == "1.1.0"
    assert (target / "toolkit" / "old.py").read_text() == "new\n"
    assert (target / "toolkit" / "added.py").read_text() == "added\n"
    assert not (target / "toolkit" / "removed.py").exists()
    assert (target / "config.yaml").read_text() == "domain: local.example\n"
    assert (target / "data" / "state.db").read_text() == "state"


def test_sync_framework_rolls_back_every_managed_file_on_failure(tmp_path: Path, monkeypatch) -> None:
    target = _snapshot(tmp_path / "target", "1.0.0", {"toolkit/a.py": "old-a", "toolkit/b.py": "old-b"})
    source = _snapshot(tmp_path / "source", "2.0.0", {"toolkit/a.py": "new-a", "toolkit/b.py": "new-b"})

    from toolkit.core.bootstrap import framework_sync

    original = framework_sync._install_staged_path
    calls = 0

    def fail_second(staged: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        original(staged, destination)

    monkeypatch.setattr(framework_sync, "_install_staged_path", fail_second)

    with pytest.raises(FrameworkSyncError, match="rolled back"):
        sync_framework(source, target)

    assert (target / "toolkit" / "a.py").read_text() == "old-a"
    assert (target / "toolkit" / "b.py").read_text() == "old-b"
    assert (target / ".homelab-framework-version").read_text() == "1.0.0\n"


def test_sync_framework_remains_recoverable_after_hard_interruption(tmp_path: Path, monkeypatch) -> None:
    target = _snapshot(tmp_path / "target", "1.0.0", {"toolkit/a.py": "old"})
    source = _snapshot(tmp_path / "source", "2.0.0", {"toolkit/a.py": "new"})

    from toolkit.core.bootstrap import framework_sync

    original = framework_sync._install_staged_path

    def interrupt_apply(_staged: Path, _destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(framework_sync, "_install_staged_path", interrupt_apply)
    with pytest.raises(KeyboardInterrupt):
        sync_framework(source, target)

    assert (target / ".homelab-framework-files.json").is_file()
    assert (target / ".homelab-framework-version").read_text() == "1.0.0\n"

    monkeypatch.setattr(framework_sync, "_install_staged_path", original)
    sync_framework(source, target)
    assert (target / "toolkit" / "a.py").read_text() == "new"


def test_sync_framework_rejects_source_checkout_and_path_traversal(tmp_path: Path) -> None:
    target = _snapshot(tmp_path / "target", "1.0.0", {"toolkit/a.py": "old"})
    source = _snapshot(tmp_path / "source", "2.0.0", {"toolkit/a.py": "new"})
    (target / ".git").mkdir()

    with pytest.raises(FrameworkSyncError, match="source checkout"):
        sync_framework(source, target)

    (target / ".git").rmdir()
    (source / ".homelab-framework-files.json").write_text('["../outside"]\n', encoding="utf-8")
    with pytest.raises(FrameworkSyncError, match="unsafe framework path"):
        sync_framework(source, target)


def test_sync_framework_rejects_overlapping_roots_and_symlinked_source_parent(tmp_path: Path) -> None:
    target = _snapshot(tmp_path / "target", "1.0.0", {"toolkit/a.py": "old"})
    nested_source = _snapshot(target / "source", "2.0.0", {"toolkit/a.py": "new"})

    with pytest.raises(FrameworkSyncError, match="must not overlap"):
        sync_framework(nested_source, target)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.py").write_text("outside", encoding="utf-8")
    source = _snapshot(tmp_path / "source", "2.0.0", {})
    (source / "linked").symlink_to(outside, target_is_directory=True)
    (source / ".homelab-framework-files.json").write_text('["linked/payload.py"]\n', encoding="utf-8")

    with pytest.raises(FrameworkSyncError, match="parent is a symlink"):
        sync_framework(source, target)
