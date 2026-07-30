"""Authelia / OIDC URL helpers — cfg-aware.

Single source of truth for the public Authelia issuer URL and OIDC verify probes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.verify.models import VerifyCheck
from toolkit.services.sdk._vmexec import docker_curl, docker_exec_on_vm

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "authelia_public_url",
    "authelia_public_url_for_domain",
    "authelia_oidc_issuer",
    "authelia_forward_auth_block",
    "oidc_check_env_issuer",
    "oidc_check_auth_discovery_route",
    "authelia_oidc_discovery",
]


def authelia_public_url_for_domain(domain: str) -> str:
    """Public HTTPS URL for Authelia when only ``domain`` is available."""
    return f"https://auth.{domain}"


def authelia_public_url(cfg: Config) -> str:
    """Public HTTPS URL for Authelia (``https://auth.<domain>``)."""
    return authelia_public_url_for_domain(cfg.domain)


def authelia_oidc_issuer(cfg: Config) -> str:
    """OIDC issuer URL — identical to :func:`authelia_public_url`."""
    return authelia_public_url(cfg)


def authelia_forward_auth_block(cfg: Config) -> list[str]:
    """Caddy ``forward_auth`` block lines for Authelia (when management is enabled)."""
    if not cfg.category_enabled("management"):
        return []
    return [
        "    forward_auth http://authelia:9091 {",
        "        uri /api/authz/forward-auth",
        "        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email",
        "    }",
    ]


def oidc_check_env_issuer(
    cfg: Config,
    service: str,
    container: str,
    env_var: str,
    vm_ip: str,
    root: Path,
    *,
    check_discovery: bool = True,
) -> list[VerifyCheck]:
    """Read a container's env, verify an OIDC issuer URL matches Authelia."""
    checks: list[VerifyCheck] = []
    expected = authelia_oidc_issuer(cfg)
    rc, out = docker_exec_on_vm(cfg, container, ["env"], vm_ip, root)
    if rc != 0:
        checks.append(VerifyCheck(service, "oidc_issuer", False, "could not read env (container not ready)"))
        return checks
    url = ""
    for line in out.splitlines():
        if line.startswith(f"{env_var}="):
            url = line.split("=", 1)[1].strip()
            break
    if not url:
        checks.append(VerifyCheck(service, "oidc_issuer", False, f"{env_var} not set in env"))
        return checks
    match = url == expected
    detail = url if match else f"WARNING: {url} (expected {expected})"
    checks.append(VerifyCheck(service, "oidc_issuer", match, detail))
    if check_discovery:
        checks.append(oidc_check_auth_discovery_route(cfg, service, root))
    return checks


def oidc_check_auth_discovery_route(cfg: Config, service: str, root: Path) -> VerifyCheck:
    """Verify OIDC clients can reach Authelia discovery without Cloudflare."""
    from toolkit.core.manifest.catalog import load_service_catalog, provider_service_name
    from toolkit.core.manifest.placement import manifest_node, service_address

    ingress_service = provider_service_name("ingress")
    infra_ip = service_address(cfg, ingress_service)
    authelia = load_service_catalog(root).require("authelia")
    authelia_ip = cfg.node_ip(manifest_node(cfg, authelia))
    internal_probe = [
        "sh",
        "-c",
        "curl -skf --max-time 15 "
        f"-H 'Host: auth.{cfg.domain}' "
        f"-H 'X-Forwarded-Proto: https' "
        f"-H 'X-Forwarded-Host: auth.{cfg.domain}' "
        f"'http://{authelia_ip}:9091/.well-known/openid-configuration'",
    ]
    rc, disc_out = docker_exec_on_vm(cfg, ingress_service, internal_probe, infra_ip, root, timeout=25)
    body = disc_out or ""
    disc_ok = rc == 0 and "token_endpoint" in body and "Just a moment" not in body
    return VerifyCheck(
        service,
        "oidc_token_route",
        disc_ok,
        "OIDC discovery reachable (bypasses Cloudflare)" if disc_ok else "token discovery blocked or unreachable",
    )


def authelia_oidc_discovery(cfg: Config, infra_ip: str, root: Path) -> tuple[dict | None, str]:
    """Fetch and parse Authelia's OIDC discovery document."""
    discovery_path = "/.well-known/openid-configuration"
    headers = {"Host": f"auth.{cfg.domain}", "X-Forwarded-Proto": "https"}

    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg, infra_ip, "authelia", f"http://localhost:9091{discovery_path}", root=root, headers=headers
        )
        ok = rc == 0 and bool(body.strip())
    else:
        import httpx

        try:
            resp = httpx.get(
                f"http://localhost:9091{discovery_path}",
                headers=headers,
                timeout=10,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            resp = None
        ok = bool(resp and resp.status_code == 200)
        body = resp.text if resp else ""

    if not ok:
        return None, "discovery endpoint unreachable"
    try:
        return json.loads(body), ""
    except json.JSONDecodeError:
        return None, "discovery returned invalid JSON"
