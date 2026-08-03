"""Redirect-target validation shared by the web UI routers."""

from __future__ import annotations

from urllib.parse import urlsplit


def local_redirect_target(location: str) -> str:
    """Allow redirects only to absolute paths on this UI origin."""
    parsed = urlsplit(location)
    if (
        not location.startswith("/")
        or location.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in location
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in location)
    ):
        return "/"
    return location
