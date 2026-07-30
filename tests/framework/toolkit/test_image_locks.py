from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import toolkit.core.images.locks as locks_module
import yaml
from toolkit.core.images.locks import (
    ImageResolutionError,
    apply_image_locks,
    discover_image_locks,
    load_image_lock_cache,
    resolve_image_locks,
    resolve_image_reference,
    save_image_lock_cache,
)


def _write_compose(root: Path) -> Path:
    service = root / "toolkit" / "services" / "example"
    service.mkdir(parents=True)
    path = service / "compose.yaml"
    path.write_text(
        """services:
  example:
    image: ghcr.io/example/service:v1.2.3
  helper:
    image: redis:8-alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  built:
    build: .
    image: ${HOMELAB_EXAMPLE_IMAGE:?generate first}
""",
        encoding="utf-8",
    )
    return path


def test_discover_image_locks_finds_only_unpinned_registry_images(tmp_path: Path) -> None:
    compose = _write_compose(tmp_path)

    locks = discover_image_locks(tmp_path)

    assert [(lock.plugin, lock.runtime, lock.version_ref) for lock in locks] == [
        ("example", "example", "ghcr.io/example/service:v1.2.3")
    ]
    assert locks[0].compose_path == compose


def test_discover_image_locks_can_include_existing_pins(tmp_path: Path) -> None:
    _write_compose(tmp_path)

    locks = discover_image_locks(tmp_path, include_pinned=True)

    assert [lock.runtime for lock in locks] == ["example", "helper"]


def test_resolve_image_reference_reads_index_digest_and_platforms() -> None:
    payload = {
        "digest": "sha256:" + ("b" * 64),
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    }
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> CompletedProcess[str]:
        calls.append(command)
        return CompletedProcess(command, 0, json.dumps(payload), "")

    resolved = resolve_image_reference("ghcr.io/example/service:v1.2.3", runner=runner)

    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.platforms == ("linux/amd64", "linux/arm64")
    assert calls == [
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "ghcr.io/example/service:v1.2.3",
            "--format",
            "{{json .Manifest}}",
        ]
    ]


def test_resolve_image_reference_retries_registry_rate_limits(monkeypatch) -> None:
    command_results = iter(
        [
            CompletedProcess([], 1, "", "429 Too Many Requests"),
            CompletedProcess([], 0, json.dumps({"digest": "sha256:" + ("b" * 64), "manifests": []}), ""),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr("toolkit.core.images.locks.time.sleep", sleeps.append)

    resolved = resolve_image_reference(
        "ghcr.io/example/service:v1.2.3",
        runner=lambda _command, **_kwargs: next(command_results),
    )

    assert resolved.digest == "sha256:" + ("b" * 64)
    assert sleeps == [1.0]


def test_resolve_image_reference_does_not_retry_permanent_errors(monkeypatch) -> None:
    calls = 0

    def runner(_command, **_kwargs):
        nonlocal calls
        calls += 1
        return CompletedProcess([], 1, "", "manifest unknown")

    monkeypatch.setattr("toolkit.core.images.locks.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="manifest unknown"):
        resolve_image_reference("ghcr.io/example/service:v1.2.3", runner=runner)

    assert calls == 1


def test_resolve_image_reference_uses_docker_hub_tag_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        locks_module,
        "_read_docker_hub_tag",
        lambda path, tag, _timeout: {
            "name": tag,
            "digest": "sha256:" + ("c" * 64),
            "images": [
                {"os": "linux", "architecture": "amd64"},
                {"os": "linux", "architecture": "arm64"},
            ],
        },
    )

    resolved = resolve_image_reference(
        "postgres:16-alpine",
        runner=lambda _command, **_kwargs: pytest.fail("Docker CLI must not resolve Docker Hub metadata"),
    )

    assert resolved.digest == "sha256:" + ("c" * 64)
    assert resolved.platforms == ("linux/amd64", "linux/arm64")


def test_apply_image_locks_updates_only_parsed_image_fields(tmp_path: Path) -> None:
    compose = _write_compose(tmp_path)
    lock = discover_image_locks(tmp_path)[0]
    digest = "sha256:" + ("b" * 64)

    changed = apply_image_locks((lock,), {lock.version_ref: digest})

    document = yaml.safe_load(compose.read_text(encoding="utf-8"))
    assert changed == (compose,)
    assert document["services"]["example"]["image"] == f"ghcr.io/example/service:v1.2.3@{digest}"
    assert document["services"]["helper"]["image"].endswith("a" * 64)


def test_resolve_image_locks_deduplicates_registry_requests(tmp_path: Path) -> None:
    _write_compose(tmp_path)
    first = discover_image_locks(tmp_path)[0]
    duplicate = first.__class__(
        plugin="another",
        runtime="another",
        compose_path=first.compose_path,
        current_ref=first.current_ref,
        version_ref=first.version_ref,
        digest=None,
    )
    calls: list[str] = []
    progress: list[tuple[int, int, str]] = []

    def resolver(version_ref: str):
        calls.append(version_ref)
        return resolve_image_reference(
            version_ref,
            runner=lambda command, **_kwargs: CompletedProcess(
                command,
                0,
                json.dumps({"digest": "sha256:" + ("b" * 64), "manifests": []}),
                "",
            ),
        )

    resolved = resolve_image_locks(
        (first, duplicate),
        resolver=resolver,
        on_progress=lambda done, total, image: progress.append((done, total, image.version_ref)),
    )

    assert list(resolved) == [first.version_ref]
    assert calls == [first.version_ref]
    assert progress == [(1, 1, first.version_ref)]


def test_resolve_image_locks_reports_partial_success_after_registry_failure(tmp_path: Path) -> None:
    _write_compose(tmp_path)
    first = discover_image_locks(tmp_path)[0]
    second = first.__class__(
        plugin="another",
        runtime="another",
        compose_path=first.compose_path,
        current_ref="ghcr.io/example/other:v2",
        version_ref="ghcr.io/example/other:v2",
        digest=None,
    )
    progress: list[str] = []

    def resolver(version_ref: str):
        if version_ref == second.version_ref:
            raise RuntimeError("registry unavailable")
        return resolve_image_reference(
            version_ref,
            runner=lambda command, **_kwargs: CompletedProcess(
                command,
                0,
                json.dumps({"digest": "sha256:" + ("b" * 64), "manifests": []}),
                "",
            ),
        )

    with pytest.raises(ImageResolutionError) as captured:
        resolve_image_locks(
            (first, second),
            resolver=resolver,
            on_progress=lambda _done, _total, image: progress.append(image.version_ref),
        )

    assert list(captured.value.resolved) == [first.version_ref]
    assert progress == [first.version_ref]
    assert second.version_ref in str(captured.value)


def test_image_lock_cache_round_trip_and_expiry(tmp_path: Path) -> None:
    resolved = resolve_image_reference(
        "ghcr.io/example/service:v1.2.3",
        runner=lambda command, **_kwargs: CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "digest": "sha256:" + ("b" * 64),
                    "manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}],
                }
            ),
            "",
        ),
    )

    save_image_lock_cache(tmp_path, {resolved.version_ref: resolved}, now=1000.0)

    assert load_image_lock_cache(tmp_path, now=1100.0) == {resolved.version_ref: resolved}
    assert load_image_lock_cache(tmp_path, now=5000.0) == {}
