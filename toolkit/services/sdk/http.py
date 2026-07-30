"""HTTP primitives — cfg-free leaf module.

Depends only on the Python stdlib + httpx. Never imports from ``toolkit.core.*``.
"""

from __future__ import annotations

import base64
import time

import httpx

__all__ = [
    "http_check",
    "http_health_check",
    "wait_for_http",
    "basic_auth_header",
]


def http_check(
    url: str,
    *,
    expected_status: int = 200,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> tuple[bool, str]:
    """GET ``url`` and compare the status code to ``expected_status``.

    Returns ``(ok, detail)``. ``detail`` is ``HTTP <code>`` on a response or
    ``unreachable: <error>`` when the request fails to connect. Redirects are
    followed, so the final response code is the one compared.
    """
    try:
        resp = httpx.get(
            url,
            headers=headers or {},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return False, f"unreachable: {exc}"
    if resp.status_code == expected_status:
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}"


def _http_reachable(url: str, *, timeout: int = 10) -> tuple[bool, str]:
    """GET ``url``; treat any status < 500 as reachable."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code < 500:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)


def http_health_check(checks: list[tuple[str, str]]) -> list[str]:
    """Probe ``(name, url)`` pairs and return human-readable log lines."""
    logs: list[str] = []
    for name, url in checks:
        ok, detail = _http_reachable(url)
        if ok:
            logs.append(f"{name}: reachable ({detail})")
        else:
            logs.append(f"{name}: not ready ({detail})")
    return logs


def wait_for_http(url: str, *, timeout: int = 60, interval: int = 5) -> bool:
    """Poll ``url`` until it returns HTTP 200, or ``timeout`` seconds elapse."""
    deadline = time.time() + timeout
    per_request = max(1, min(interval, 10))
    while time.time() < deadline:
        ok, _ = http_check(url, timeout=per_request)
        if ok:
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return False


def basic_auth_header(username: str, password: str) -> str:
    """Return an HTTP ``Authorization`` header value: ``Basic <base64>``."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"
