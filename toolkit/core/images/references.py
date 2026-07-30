"""Strict OCI image-reference parsing shared by service catalog tooling."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class ImageVersionReference:
    repository: str
    tag: str

    @property
    def version_ref(self) -> str:
        return f"{self.repository}:{self.tag}"


@dataclass(frozen=True, slots=True)
class ImmutableImageReference(ImageVersionReference):
    digest: str

    @property
    def immutable_ref(self) -> str:
        return f"{self.version_ref}@{self.digest}"


def parse_image_version_reference(value: str) -> ImageVersionReference:
    """Parse a non-floating tagged image reference without a digest."""
    if not isinstance(value, str) or len(value) > 512 or "@" in value:
        raise ValueError("image reference must contain one repository and version tag")
    separator = value.rfind(":")
    if separator <= value.rfind("/"):
        raise ValueError("image reference must include an explicit version tag")
    repository, tag = value[:separator], value[separator + 1 :]
    if not _REPOSITORY.fullmatch(repository) or not _TAG.fullmatch(tag):
        raise ValueError("image reference has an invalid repository or tag")
    if tag.lower() == "latest":
        raise ValueError("image reference cannot use the mutable latest tag")
    return ImageVersionReference(repository=repository, tag=tag)


def parse_immutable_image_reference(value: str) -> ImmutableImageReference:
    """Parse a tagged, digest-pinned image reference or fail closed."""
    if not isinstance(value, str) or len(value) > 512 or value.count("@") != 1:
        raise ValueError("image reference must contain exactly one digest")
    tagged, digest = value.split("@", 1)
    version = parse_image_version_reference(tagged)
    if not _DIGEST.fullmatch(digest):
        raise ValueError("image reference must use a lowercase SHA-256 digest")
    return ImmutableImageReference(repository=version.repository, tag=version.tag, digest=digest)
