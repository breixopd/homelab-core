"""Strict curl config rendering for credential-safe process execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlparse

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_METHOD_RE = re.compile(r"^[A-Z]+$")


def _config_quote(value: str) -> str:
    """Quote one curl config-file value using curl's documented escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\v", "\\v")
    )


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("curl URL must be a non-empty HTTP(S) URL without control characters")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("curl URL must be an HTTP(S) URL without embedded credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("curl URL must contain a valid port") from exc


def _validate_headers(headers: Mapping[str, str]) -> None:
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError("curl header name is invalid")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("curl header value contains an unsafe control character")


def _validate_absolute_path(path: str | None, *, label: str) -> None:
    if path is None:
        return
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError(f"curl {label} must be an absolute path without control characters")


def render_curl_config(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: int = 15,
    insecure_tls: bool = False,
    ca_file: str | None = None,
    cookie_file: str | None = None,
    cookie_jar: str | None = None,
) -> str:
    """Render one validated curl request config.

    Curl accepts ``--config -`` to read this content from stdin. The caller must
    invoke curl with ``--disable`` before ``--config`` so a user-controlled
    curlrc cannot alter a privileged service request.
    """
    _validate_url(url)
    if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
        raise ValueError("curl method must contain only uppercase letters")
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("curl timeout must be between 1 and 300 seconds")
    if body is not None and not isinstance(body, str):
        raise ValueError("curl request body must be text")
    validated_headers = headers or {}
    _validate_headers(validated_headers)
    _validate_absolute_path(ca_file, label="CA bundle path")
    _validate_absolute_path(cookie_file, label="cookie file path")
    _validate_absolute_path(cookie_jar, label="cookie jar path")

    lines = ["silent", "show-error", "fail", "location", f"max-time = {timeout}", f'request = "{method}"']
    if insecure_tls:
        lines.append("insecure")
    if ca_file:
        lines.append(f'cacert = "{_config_quote(ca_file)}"')
    if cookie_file:
        lines.append(f'cookie = "{_config_quote(cookie_file)}"')
    if cookie_jar:
        lines.append(f'cookie-jar = "{_config_quote(cookie_jar)}"')
    for name, value in validated_headers.items():
        header = f"{name}: {value}"
        lines.append(f'header = "{_config_quote(header)}"')
    if body is not None:
        lines.append(f'data-raw = "{_config_quote(body)}"')
    lines.append(f'url = "{_config_quote(url)}"')
    return "\n".join(lines) + "\n"
