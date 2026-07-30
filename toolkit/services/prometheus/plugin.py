"""prometheus service plugin.

Owns verify() for Prometheus health/readiness and scrape-target status on top
of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

import json
import re
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

    _TARGETS_BODY_LIMIT = 512 * 1024
    _RESOURCE_LIMIT = 100
    _TEXT_LIMIT = 200

    @classmethod
    def _safe_target_text(cls, value: object) -> str:
        """Return a short, printable target field with credential-like values redacted."""
        if not isinstance(value, str):
            return ""
        text = "".join(char for char in value.strip() if ord(char) >= 32 and ord(char) != 127)
        # Prometheus errors can echo a scrape URL. Never expose URL userinfo in the
        # service-management projection, even when the API returned it verbatim.
        text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
        from toolkit.controller.sanitization import sanitize_message

        return sanitize_message(text)[: cls._TEXT_LIMIT]

    def _target_snapshot(self, cfg: Config, root: Path) -> list[dict[str, object]]:
        """Read the bounded active-target projection from Prometheus' stable API."""
        from toolkit.services.sdk import docker_curl

        rc, body = docker_curl(
            cfg,
            self.runtime_address(cfg),
            "prometheus",
            "http://localhost:9090/api/v1/targets",
            root=root,
            timeout=5,
        )
        if rc != 0 or not body or len(body.encode("utf-8", errors="replace")) > self._TARGETS_BODY_LIMIT:
            raise RuntimeError("Prometheus targets API is unavailable")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("Prometheus targets API returned invalid data") from exc
        data = payload.get("data") if isinstance(payload, dict) and payload.get("status") == "success" else None
        active = data.get("activeTargets") if isinstance(data, dict) else None
        if not isinstance(active, list):
            raise RuntimeError("Prometheus targets API returned invalid data")
        # The response byte limit bounds the parsed collection. Resource projection
        # is capped separately, while status counts remain exact for accepted data.
        return [target for target in active if isinstance(target, dict)]

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        try:
            targets = self._target_snapshot(cfg, root)
        except RuntimeError:
            return {}
        healthy = sum(target.get("health") == "up" for target in targets)
        return {
            "target_count": len(targets),
            "healthy_targets": healthy,
            "unhealthy_targets": len(targets) - healthy,
        }

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        targets = self._target_snapshot(cfg, root)
        rows: list[dict[str, object]] = []
        for target in targets[: self._RESOURCE_LIMIT]:
            labels = target.get("labels")
            labels = labels if isinstance(labels, dict) else {}
            rows.append(
                {
                    "job": self._safe_target_text(labels.get("job")),
                    "health": self._safe_target_text(target.get("health")),
                    "last_scrape": self._safe_target_text(target.get("lastScrape")),
                    "last_error": self._safe_target_text(target.get("lastError")),
                }
            )
        return {"targets": rows}

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
