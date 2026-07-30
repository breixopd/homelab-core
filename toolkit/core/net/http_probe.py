"""HTTP(S) probes with IPv4-first transport (avoids broken IPv6 hangs)."""

from __future__ import annotations

import httpx

_DEFAULT_TIMEOUT = 15.0
_client: httpx.Client | None = None


def http_client(*, timeout: float = _DEFAULT_TIMEOUT, retries: int = 1) -> httpx.Client:
    """IPv4-first httpx client for external HTTP(S) probes."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=retries)
    return httpx.Client(
        transport=transport,
        follow_redirects=True,
        timeout=timeout,
        verify=True,
    )


def _client_get() -> httpx.Client:
    global _client
    if _client is None:
        _client = http_client()
    return _client


def probe_url(url: str, *, timeout: float = _DEFAULT_TIMEOUT, method: str = "HEAD") -> tuple[bool, str]:
    """Return (ok, detail) for an HTTPS URL. 401/403 count as reachable."""
    client = _client_get()
    try:
        resp = client.request(method, url, timeout=timeout)
        code = resp.status_code
        if code in (200, 301, 302, 401, 403):
            return True, str(code)
        if method == "HEAD" and code == 405:
            resp = client.get(url, timeout=timeout)
            code = resp.status_code
            if code in (200, 301, 302, 401, 403):
                return True, str(code)
        return False, f"HTTP {code}"
    except httpx.HTTPError as exc:
        return False, str(exc)[:120]
