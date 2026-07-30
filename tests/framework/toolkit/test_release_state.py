from __future__ import annotations

import json
from pathlib import Path

import pytest
from toolkit.core.ops.release_state import (
    ReleaseStateError,
    build_release,
    load_active_release,
    load_recovery_release,
    load_rollback_release,
    write_active_release,
    write_recovery_release,
    write_rollback_release,
)

IMAGE = "docker.io/library/redis@sha256:" + ("a" * 64)
VERSION = "docker.io/library/redis:8.8.0"


def test_release_state_round_trip_is_revisioned_and_digest_only(tmp_path: Path) -> None:
    release = build_release({"redis": IMAGE}, {"redis": VERSION}, created_at="2026-07-12T00:00:00+00:00")

    path = write_active_release(tmp_path, release)

    assert path == tmp_path / ".homelab-state" / "releases" / "active.json"
    assert load_active_release(tmp_path) == release
    assert len(release.revision) == 64


def test_release_state_rejects_mutated_content(tmp_path: Path) -> None:
    release = build_release({"redis": IMAGE}, {"redis": VERSION}, created_at="2026-07-12T00:00:00+00:00")
    path = write_active_release(tmp_path, release)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["images"]["redis"] = "docker.io/library/redis@sha256:" + ("b" * 64)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseStateError, match="revision"):
        load_active_release(tmp_path)


@pytest.mark.parametrize(
    "images",
    [
        {"redis": "redis:8"},
        {"UPPER": IMAGE},
        {"redis": "docker.io/library/redis@sha256:short"},
        {"redis": "docker.io/library/redis@sha256:" + ("A" * 64)},
    ],
)
def test_release_state_rejects_noncanonical_images(images: dict[str, str]) -> None:
    with pytest.raises(ReleaseStateError):
        build_release(images, {next(iter(images)): VERSION}, created_at="2026-07-12T00:00:00+00:00")


def test_empty_release_removes_active_state(tmp_path: Path) -> None:
    write_active_release(
        tmp_path,
        build_release({"redis": IMAGE}, {"redis": VERSION}, created_at="2026-07-12T00:00:00+00:00"),
    )

    write_active_release(tmp_path, None)

    assert load_active_release(tmp_path) is None


def test_rollback_state_can_restore_the_unpinned_base_release(tmp_path: Path) -> None:
    current = build_release({"redis": IMAGE}, {"redis": VERSION}, created_at="2026-07-12T00:00:00+00:00")

    write_rollback_release(tmp_path, expected_active_revision=current.revision, previous=None)

    rollback = load_rollback_release(tmp_path)
    assert rollback is not None
    assert rollback.expected_active_revision == current.revision
    assert rollback.previous is None


def test_recovery_state_binds_a_failed_release_to_the_exact_restore_target(tmp_path: Path) -> None:
    previous = build_release({"redis": IMAGE}, {"redis": VERSION}, created_at="2026-07-12T00:00:00+00:00")
    failed = build_release(
        {"redis": "docker.io/library/redis@sha256:" + ("b" * 64)},
        {"redis": "docker.io/library/redis:8.9.0"},
        created_at="2026-07-13T00:00:00+00:00",
    )

    path = write_recovery_release(tmp_path, previous=previous, failed=failed)

    recovery = load_recovery_release(tmp_path)
    assert path == tmp_path / ".homelab-state" / "releases" / "recovery.json"
    assert recovery is not None
    assert recovery.previous == previous
    assert recovery.failed == failed
