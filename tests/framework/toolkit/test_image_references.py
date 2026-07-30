from __future__ import annotations

import pytest
from toolkit.core.images.references import (
    ImageVersionReference,
    ImmutableImageReference,
    parse_image_version_reference,
    parse_immutable_image_reference,
)


def test_parse_immutable_image_reference_preserves_tag_and_digest() -> None:
    digest = "sha256:" + ("a" * 64)

    reference = parse_immutable_image_reference(f"ghcr.io/example/service:v1.2.3@{digest}")

    assert reference == ImmutableImageReference(
        repository="ghcr.io/example/service",
        tag="v1.2.3",
        digest=digest,
    )
    assert reference.version_ref == "ghcr.io/example/service:v1.2.3"
    assert reference.immutable_ref == f"ghcr.io/example/service:v1.2.3@{digest}"


def test_parse_image_version_reference_accepts_tag_without_digest() -> None:
    assert parse_image_version_reference("registry.example:5000/team/service:1.2") == ImageVersionReference(
        repository="registry.example:5000/team/service",
        tag="1.2",
    )


@pytest.mark.parametrize(
    "reference",
    [
        "postgres:16-alpine",
        "postgres@sha256:" + ("a" * 64),
        "postgres:latest@sha256:" + ("a" * 64),
        "POSTGRES:16@sha256:" + ("a" * 64),
        "postgres:16@sha512:" + ("a" * 128),
        "${POSTGRES_IMAGE:?generate first}",
    ],
)
def test_parse_immutable_image_reference_rejects_mutable_or_ambiguous_references(reference: str) -> None:
    with pytest.raises(ValueError, match="image reference"):
        parse_immutable_image_reference(reference)
