"""prometheus service plugin.

Owns verify() for Prometheus health/readiness and scrape-target status on top
of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


class PrometheusPlugin(ServicePlugin):
    service = "prometheus"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.manifest.monitoring import compile_prometheus_targets

        scrape_jobs: dict[tuple[str, str], dict[str, object]] = {}
        for target in compile_prometheus_targets(context.config):
            job = scrape_jobs.setdefault(
                (target.job, target.path),
                {"name": target.job, "path": target.path, "targets": []},
            )
            targets = job["targets"]
            if not isinstance(targets, list):
                raise RuntimeError(f"Prometheus job {target.job!r} has an invalid target accumulator")
            targets.append({"address": target.target, "instance": target.instance})
        context.render_template(
            "generated/prometheus.yml",
            "prometheus.yml.j2",
            {"service_scrapes": list(scrape_jobs.values())},
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Wait for the targets API and summarize scrape readiness."""
        import httpx
        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.services.sdk import wait_for_http

        targets_url = f"{resolve_docker_service_url('prometheus', 9090)}/api/v1/targets"
        if not wait_for_http(targets_url, timeout=60, interval=5):
            return ["WARNING: Prometheus not reachable yet (targets API timeout)"]
        try:
            response = httpx.get(targets_url, timeout=10, follow_redirects=True)
            response.raise_for_status()
            active = response.json().get("data", {}).get("activeTargets", [])
            healthy = sum(1 for target in active if target.get("health") == "up")
            return [f"Prometheus: {healthy}/{len(active)} targets up"]
        except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
            return [f"WARNING: Prometheus targets unavailable ({exc})"]

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl
        from toolkit.services.sdk.monitoring import prometheus_internal_url

        checks: list[VerifyCheck] = []

        if not container_exists_on_vm(cfg, vm_ip, "prometheus", root):
            return [
                VerifyCheck("prometheus", "healthy", False, "container missing"),
                VerifyCheck("prometheus", "ready", False, "container missing"),
                VerifyCheck("prometheus", "targets", False, "container missing"),
            ]

        base = prometheus_internal_url()
        for path, check in (("/-/healthy", "healthy"), ("/-/ready", "ready")):
            rc, body = docker_curl(cfg, vm_ip, "prometheus", f"{base}{path}", root=root)
            ok = rc == 0
            detail = (body or "").strip()[:80] if body else ("ok" if ok else "unreachable")
            checks.append(VerifyCheck("prometheus", check, ok, detail))

        rc, body = docker_curl(cfg, vm_ip, "prometheus", f"{base}/api/v1/targets", root=root)
        if rc != 0 or not body:
            checks.append(VerifyCheck("prometheus", "targets", False, "API unreachable"))
            return checks
        try:
            active = json.loads(body).get("data", {}).get("activeTargets", [])
        except json.JSONDecodeError:
            checks.append(VerifyCheck("prometheus", "targets", False, "invalid targets JSON"))
            return checks

        down = [t for t in active if t.get("health") != "up"]
        down_labels = []
        for target in down[:5]:
            labels = target.get("labels") or {}
            down_labels.append(labels.get("job") or labels.get("instance") or "unknown")
        detail = f"{len(active) - len(down)}/{len(active)} targets up"
        if down:
            detail += f" (down: {', '.join(down_labels)}{'…' if len(down) > 5 else ''})"
        checks.append(VerifyCheck("prometheus", "targets", len(down) == 0, detail))
        return checks
