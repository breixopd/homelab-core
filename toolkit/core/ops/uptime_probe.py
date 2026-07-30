"""External synthetic uptime probes — hit public endpoints via the real internet path.

Unlike ``verify`` which probes from the controller (and may use loopback or
Docker DNS), these probes go through the public DNS → Cloudflare → Caddy path,
catching cert renewal failures, Cloudflare misconfig, and public reachability
issues that controller-side checks miss.

The results feed into the audit log + ntfy alerts, so the operator learns
about a public outage even when sitting on the LAN.

Usage:
    from toolkit.core.ops.uptime_probe import probe_public_endpoints

    results = probe_public_endpoints(cfg, root)
    for r in results:
        if not r.ok:
            print(f"{r.url}: {r.detail}")
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeResult:
    """One endpoint probe result."""

    service: str
    url: str
    ok: bool
    status_code: int
    detail: str
    latency_ms: float


def _build_probe_urls(cfg: Config, catalog: ServiceCatalog | None = None) -> list[tuple[str, str]]:
    """Return (service, full_url) pairs for all public endpoints."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import public_routes

    selected = catalog or load_service_catalog()
    probes: list[tuple[str, str]] = []
    for route in public_routes(cfg, selected):
        if route.match is not None:
            continue
        try:
            path = selected.require(route.service).health.public_probe_path
        except KeyError:
            path = ""
        if path:
            probes.append((route.service, f"https://{route.host}{path}"))
    return probes


def probe_public_endpoints(
    cfg: Config,
    root: Path,
    *,
    timeout: float = 15.0,
    on_result=None,
) -> list[ProbeResult]:
    """Probe all public endpoints via the real internet path.

    Returns one ProbeResult per endpoint. Network failures are non-fatal —
    they're recorded as ok=False with the error detail.
    """
    probes = _build_probe_urls(cfg)
    results: list[ProbeResult] = []
    for service, url in probes:
        t0 = time.monotonic()
        try:
            # Keep redirects visible: a forward-auth challenge is healthy, while
            # a wildcard/missing Caddy route returning 404 is not.
            resp = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": "homelab-uptime-probe/1.0"},
            )
            latency = round((time.monotonic() - t0) * 1000, 0)
            ok = 200 <= resp.status_code < 400
            detail = f"HTTP {resp.status_code}"
            results.append(ProbeResult(service, url, ok, resp.status_code, detail, latency))
        except httpx.HTTPError as exc:
            latency = round((time.monotonic() - t0) * 1000, 0)
            results.append(ProbeResult(service, url, False, 0, f"network error: {type(exc).__name__}", latency))
        if on_result:
            on_result(results[-1])
    return results


def run_uptime_probe(cfg: Config, root: Path) -> dict:
    """Probe all public endpoints, log to audit, notify on failures.

    Returns a summary dict suitable for the CLI/WebUI.
    """
    from toolkit.core.state.audit_log import AuditAction, audit
    from toolkit.services.ntfy.client import post_ntfy_url

    t0 = time.time()
    results = probe_public_endpoints(cfg, root)
    failed = [r for r in results if not r.ok]
    ok = len(failed) == 0

    audit(
        root,
        AuditAction.WATCHDOG,
        actor="uptime-probe",
        ok=ok,
        detail=f"{len(results) - len(failed)}/{len(results)} endpoints reachable",
        duration_s=round(time.time() - t0, 1),
        extra={
            "failed": [{"service": r.service, "url": r.url, "detail": r.detail} for r in failed],
        },
    )

    # ntfy alert on failures (best-effort).
    if failed and cfg.notifications.deploy_ntfy_url:
        msg_lines = [f"⚠ Uptime probe: {len(failed)} endpoint(s) down"]
        for r in failed[:5]:
            msg_lines.append(f"  • {r.service}: {r.detail}")
        try:
            post_ntfy_url(cfg.notifications.deploy_ntfy_url, "\n".join(msg_lines), title="Homelab uptime probe")
        except Exception:
            pass  # best-effort

    return {
        "total": len(results),
        "ok": len(results) - len(failed),
        "failed": len(failed),
        "results": [asdict(r) for r in results],
    }
