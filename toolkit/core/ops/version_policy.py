"""Conservative version-channel policy for unattended image updates."""

from __future__ import annotations

import re

_VERSION_TAG = re.compile(r"^(?P<prefix>v?)(?P<version>\d+(?:\.\d+)+)(?P<flavor>(?:[-_].+)?)$")


def select_latest_compatible(tags: list[str], current_tag: str) -> str | None:
    """Select the newest same-major tag without changing prefix or image flavor."""
    current = _VERSION_TAG.fullmatch(current_tag)
    if current is None:
        return None
    current_version = tuple(int(part) for part in current.group("version").split("."))
    compatible: list[tuple[tuple[int, ...], str]] = []
    for tag in tags:
        candidate = _VERSION_TAG.fullmatch(tag)
        if candidate is None:
            continue
        version = tuple(int(part) for part in candidate.group("version").split("."))
        if (
            version[0] == current_version[0]
            and (current_version[0] != 0 or version[1] == current_version[1])
            and version > current_version
            and candidate.group("prefix") == current.group("prefix")
            and candidate.group("flavor") == current.group("flavor")
        ):
            compatible.append((version, tag))
    if not compatible:
        return None
    return max(compatible)[1]
