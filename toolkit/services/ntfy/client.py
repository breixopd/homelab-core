"""Unified ntfy push notifications (local container or ntfy.sh)."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx


def normalize_ntfy_url(raw: str) -> str:
    """Accept full topic URL or bare topic name."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        if not parsed.netloc:
            return ""
        path = (parsed.path or "").strip("/")
        if not path:
            return raw.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}/{path.split('/')[0]}"
    topic = raw.lstrip("/")
    return f"https://ntfy.sh/{topic}"


class NtfyClient:
    """Client for ntfy push notification service."""

    def __init__(self, base_url: str = "http://ntfy:80"):
        self.base_url = base_url.rstrip("/")

    def send(
        self,
        topic: str,
        message: str,
        *,
        title: str = "",
        priority: str = "default",
        tags: str = "",
        timeout: float = 10.0,
    ) -> bool:
        headers: dict[str, str] = {"Priority": priority}
        if title:
            headers["Title"] = title[:250]
        if tags:
            headers["Tags"] = tags
        try:
            resp = httpx.post(
                f"{self.base_url}/{topic.lstrip('/')}",
                content=message.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
            return resp.status_code in (200, 201)
        except httpx.HTTPError:
            return False

    def health(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/v1/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


def post_ntfy_url(
    post_url: str,
    message: str,
    *,
    title: str = "",
    priority: str = "default",
    tags: str = "",
    extra_headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    trust_env: bool = True,
) -> bool:
    """POST to a full ntfy topic URL (deploy notifications, external ntfy.sh)."""
    url = normalize_ntfy_url(post_url)
    if not url:
        return False
    headers: dict[str, str] = {"Priority": priority}
    if title:
        headers["Title"] = title[:250]
    if tags:
        headers["Tags"] = tags
    if extra_headers:
        headers.update(extra_headers)
    try:
        if trust_env:
            resp = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=timeout)
        else:
            with httpx.Client(trust_env=False) as client:
                resp = client.post(url, content=message.encode("utf-8"), headers=headers, timeout=timeout)
        return resp.status_code in (200, 201)
    except httpx.HTTPError:
        return False


def resolve_local_ntfy_base() -> str:
    """Docker DNS name for the infra ntfy container."""
    from toolkit.core.ops.automation import resolve_docker_service_url

    return resolve_docker_service_url("ntfy", 80)


def resolve_infra_ntfy_url(cfg) -> str:
    """Reach ntfy from another node through its published host port."""
    if cfg.is_multi_node:
        from toolkit.core.manifest.placement import service_address, service_endpoint_port

        return f"http://{service_address(cfg, 'ntfy')}:{service_endpoint_port('ntfy', published=True)}"
    return resolve_local_ntfy_base()
