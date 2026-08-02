from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.ops.dump_repository import DumpNotFoundError, DumpRepository


def _write_dump(directory: Path, name: str = "pre-deploy-20260709-120000.sql.gz") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"postgres dump")
    return path


def test_local_repository_returns_stable_content_derived_id(tmp_path: Path) -> None:
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    path = _write_dump(dump_dir)

    first = DumpRepository.local(dump_dir).list()[0]
    second = DumpRepository.local(dump_dir).list()[0]

    assert first.dump_id == second.dump_id
    assert first.path == str(path.resolve())
    assert first.sha256

    path.write_bytes(b"changed dump")
    changed = DumpRepository.local(dump_dir).list()[0]
    assert changed.dump_id != first.dump_id


@pytest.mark.parametrize(
    "path",
    [
        "/opt/homelab/generated/pre-deploy-dumps/../../etc/shadow",
        "/opt/homelab/generated/pre-deploy-dumps/not-a-dump.sql.gz",
        "/tmp/pre-deploy-20260709-120000.sql.gz",
        "/opt/homelab/generated/pre-deploy-dumps/pre-deploy-20260709-120000.sql.gz;id",
    ],
)
def test_remote_repository_rejects_untrusted_paths(path: str) -> None:
    repository = DumpRepository.remote(
        "/opt/homelab/generated/pre-deploy-dumps",
        [{"path": path, "size_bytes": 12, "sha256": "a" * 64}],
    )

    assert repository.list() == []


def test_remote_repository_rejects_empty_artifact() -> None:
    repository = DumpRepository.remote(
        "/opt/homelab/generated/pre-deploy-dumps",
        [
            {
                "path": "/opt/homelab/generated/pre-deploy-dumps/pre-deploy-20260709-120000.sql.gz",
                "size_bytes": 0,
                "sha256": "a" * 64,
            }
        ],
    )

    assert repository.list() == []


def test_resolve_rejects_unknown_dump_id(tmp_path: Path) -> None:
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    _write_dump(dump_dir)

    with pytest.raises(DumpNotFoundError):
        DumpRepository.local(dump_dir).resolve("not-a-real-id")


def test_resolve_rehashes_local_dump_before_returning(tmp_path: Path) -> None:
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    path = _write_dump(dump_dir)
    repository = DumpRepository.local(dump_dir)
    record = repository.list()[0]

    path.write_bytes(b"tampered")

    with pytest.raises(DumpNotFoundError):
        repository.resolve(record.dump_id)
