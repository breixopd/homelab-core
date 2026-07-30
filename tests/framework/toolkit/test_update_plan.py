from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from toolkit.core.config.config import Config
from toolkit.core.ops.update_plan import (
    UpdatePlanError,
    build_update_plan,
    load_current_update_plan,
    write_update_scan_compose,
)


def _report(**overrides):
    item = {
        "service": "redis",
        "image": "redis:8.8.0-alpine",
        "current": "8.8.0-alpine",
        "latest": "8.9.0-alpine",
        "needs_update": True,
        "changelog_url": "https://example.test/redis",
    }
    item.update(overrides)
    return [item]


def test_update_plan_is_revisioned_and_preserves_image_channel() -> None:
    plan = build_update_plan(
        _report(),
        {"redis": "redis:8.8.0-alpine"},
        checked_at="2026-07-12T00:00:00+00:00",
    )

    assert len(plan.revision) == 64
    assert plan.candidates[0].target_image == "redis:8.9.0-alpine"
    assert plan.candidates[0].target == "8.9.0-alpine"


def test_update_plan_accepts_digest_pinned_current_image_and_emits_tagged_target() -> None:
    current_image = "grafana/grafana:13.1.0@sha256:" + ("a" * 64)
    plan = build_update_plan(
        _report(service="grafana", image=current_image, current="13.1.0", latest="13.1.1"),
        {"grafana": current_image},
        checked_at="2026-07-12T00:00:00+00:00",
    )

    assert plan.candidates[0].current_image == current_image
    assert plan.candidates[0].target_image == "grafana/grafana:13.1.1"


@pytest.mark.parametrize(
    "report,current_images",
    [
        (_report(latest="base3"), {"redis": "redis:8.8.0-alpine"}),
        (_report(latest="9.0.0-alpine"), {"redis": "redis:8.8.0-alpine"}),
        (_report(latest="8.9.0-bookworm"), {"redis": "redis:8.8.0-alpine"}),
        (_report(), {"redis": "redis:8.7.0-alpine"}),
        (_report(image="${REDIS_IMAGE}"), {"redis": "${REDIS_IMAGE}"}),
    ],
)
def test_update_plan_rejects_unsafe_or_stale_candidates(report, current_images) -> None:
    with pytest.raises(UpdatePlanError):
        build_update_plan(report, current_images, checked_at="2026-07-12T00:00:00+00:00")


def test_update_plan_ignores_entries_without_updates() -> None:
    plan = build_update_plan(
        _report(needs_update=False, latest="8.8.0-alpine"),
        {"redis": "redis:8.8.0-alpine"},
        checked_at="2026-07-12T00:00:00+00:00",
    )

    assert plan.candidates == ()


def test_current_update_plan_rejects_stale_candidate_for_disabled_service(tmp_path, monkeypatch) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "updates-cache.json").write_text(
        json.dumps(
            {
                "cached_at": 1_784_160_000,
                "updates": _report(
                    service="media-cache",
                    image="ghcr.io/example/media-cache:1.0.0",
                    current="1.0.0",
                    latest="1.1.0",
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "toolkit.core.generate.compose_assemble.assemble_compose_text",
        lambda *_args, **_kwargs: yaml.safe_dump({"services": {"redis": {"image": "redis:8.8.0-alpine"}}}),
    )
    monkeypatch.setattr("toolkit.core.generate.compose_assemble.apply_manifest_release_versions", lambda *_args: None)
    monkeypatch.setattr(
        "toolkit.core.ops.release_state.load_active_release",
        lambda _root: SimpleNamespace(
            versions={
                "redis": "redis:8.8.0-alpine",
                "media-cache": "ghcr.io/example/media-cache:1.0.0",
            }
        ),
    )

    with pytest.raises(UpdatePlanError, match="no longer matches"):
        load_current_update_plan(tmp_path, Config())


def test_update_scan_compose_ignores_active_release_for_disabled_service(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.generate.compose_assemble.assemble_compose_text",
        lambda *_args, **_kwargs: yaml.safe_dump({"services": {"redis": {"image": "redis:8.8.0-alpine"}}}),
    )
    monkeypatch.setattr("toolkit.core.generate.compose_assemble.apply_manifest_release_versions", lambda *_args: None)
    monkeypatch.setattr(
        "toolkit.core.ops.release_state.load_active_release",
        lambda _root: SimpleNamespace(
            versions={
                "redis": "redis:8.8.0-alpine",
                "media-cache": "ghcr.io/example/media-cache:1.0.0",
            }
        ),
    )

    path = write_update_scan_compose(tmp_path, Config())

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["services"] == {"redis": {"image": "redis:8.8.0-alpine"}}
