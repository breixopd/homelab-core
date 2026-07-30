from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from tests.helpers.machines import renamed_default_machines
from toolkit.core.config.config import Config
from toolkit.core.ops.release_state import build_release
from toolkit.core.ops.release_update import (
    ReleaseUpdateError,
    affected_roles,
    build_updated_release,
    resolve_target_digest,
)
from toolkit.core.ops.update_plan import UpdateCandidate


def _candidate() -> UpdateCandidate:
    return UpdateCandidate(
        service="redis",
        current="8.8.0-alpine",
        target="8.9.0-alpine",
        current_image="redis:8.8.0-alpine",
        target_image="redis:8.9.0-alpine",
        changelog_url="",
    )


def test_resolve_target_digest_uses_the_registry_manifest_digest() -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='"sha256:' + ("a" * 64) + '"\n', stderr="")

    resolved = resolve_target_digest(_candidate(), run=run)

    assert resolved == "redis@sha256:" + ("a" * 64)
    assert calls == [
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "redis:8.9.0-alpine",
            "--format",
            "{{json .Manifest.Digest}}",
        ]
    ]


def test_resolve_target_digest_rejects_failed_or_malformed_resolution() -> None:
    for result in (
        subprocess.CompletedProcess([], 1, stdout="", stderr="denied"),
        subprocess.CompletedProcess([], 0, stdout='"sha256:short"', stderr=""),
    ):
        with pytest.raises(ReleaseUpdateError):
            resolve_target_digest(_candidate(), run=lambda *_args, **_kwargs: result)


def test_updated_release_merges_existing_digest_pins() -> None:
    prior = build_release(
        {"postgres": "postgres@sha256:" + ("b" * 64)},
        {"postgres": "postgres:16.8-alpine"},
        created_at="2026-07-11T00:00:00+00:00",
    )

    release = build_updated_release(
        prior,
        {"redis": "redis@sha256:" + ("a" * 64)},
        {"redis": "redis:8.9.0-alpine"},
        created_at="2026-07-12T00:00:00+00:00",
    )

    assert release.images == {
        "postgres": "postgres@sha256:" + ("b" * 64),
        "redis": "redis@sha256:" + ("a" * 64),
    }
    assert release.versions == {
        "postgres": "postgres:16.8-alpine",
        "redis": "redis:8.9.0-alpine",
    }


def test_single_node_update_targets_configured_control_machine(tmp_path) -> None:
    machines = renamed_default_machines()
    cfg = Config(machines={"core": machines["core"]})
    with patch(
        "toolkit.core.generate.compose_assemble.assemble_compose_text",
        return_value="services:\n  redis:\n    image: redis:8\n",
    ):
        roles = affected_roles(tmp_path, cfg, {"redis"})

    assert roles == ("core",)
