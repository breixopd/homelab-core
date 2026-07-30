from __future__ import annotations

from toolkit.core.ops.version_policy import select_latest_compatible


def test_latest_tag_must_be_a_newer_version_on_the_same_major() -> None:
    tags = ["base3", "1.0.0", "1.2.0", "2.0.0", "1.3.0-rc.1"]

    assert select_latest_compatible(tags, "1.0.0") == "1.2.0"


def test_floating_major_channel_is_not_rewritten_to_an_unrelated_tag() -> None:
    assert select_latest_compatible(["base3", "1.2.0", "2.0.0"], "1") is None


def test_image_flavor_is_preserved() -> None:
    tags = ["16.8-alpine", "16.9", "16.9-bookworm", "16.9-alpine", "17.0-alpine"]

    assert select_latest_compatible(tags, "16.8-alpine") == "16.9-alpine"


def test_v_prefix_style_is_preserved() -> None:
    tags = ["1.4.0", "v1.3.1", "v1.4.0", "v2.0.0"]

    assert select_latest_compatible(tags, "v1.3.0") == "v1.4.0"


def test_date_and_named_channels_require_manual_review() -> None:
    assert select_latest_compatible(["2026-06-01-debian"], "2026-05-05-debian") is None
    assert select_latest_compatible(["latest", "stable"], "latest") is None


def test_current_or_older_tags_never_produce_a_downgrade() -> None:
    assert select_latest_compatible(["v3.0.1", "v3.3.0"], "v3.3.0") is None
    assert select_latest_compatible(["2.1.0", "2.2.0"], "2.2.0") is None


def test_zero_major_updates_stay_on_the_current_minor_channel() -> None:
    assert select_latest_compatible(["0.63.2", "0.61.3"], "0.61.2") == "0.61.3"
