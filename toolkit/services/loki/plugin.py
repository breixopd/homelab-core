"""loki service plugin — defaults from service.yaml; override post_start/verify/heal when needed."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class LokiPlugin(ServicePlugin):
    service = "loki"
    category = "management"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, ssh_on_vm

        def _loki_get(path: str, *, timeout: int = 15) -> tuple[int, str]:
            """Query Loki via bridge IP — the image has no shell for docker exec curl."""
            shell = (
                "ip=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}} {{end}}' loki "
                "| awk '{print $1}'); "
                f'curl -sf --max-time {timeout} "http://${{ip}}:3100{path}"'
            )
            rc, out, _ = ssh_on_vm(cfg, vm_ip, shell, root=root, timeout=timeout + 5)
            return rc, out or ""

        checks: list[VerifyCheck] = []
        if cfg.domain == "localhost":
            return [VerifyCheck("loki", "ready", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "loki", root):
            return [VerifyCheck("loki", "ready", False, "container missing")]

        rc, body = _loki_get("/ready")
        ready_ok = rc == 0
        checks.append(VerifyCheck("loki", "ready", ready_ok, (body or "ok")[:80] if ready_ok else "not ready"))

        rc, body = _loki_get("/loki/api/v1/labels")
        labels_ok = False
        label_detail = "API unreachable"
        if rc == 0 and body:
            try:
                labels = json.loads(body).get("data", [])
                labels_ok = bool(labels)
                label_detail = f"{len(labels)} label(s)"
            except json.JSONDecodeError:
                label_detail = "invalid labels JSON"
        checks.append(VerifyCheck("loki", "labels", labels_ok, label_detail))

        now_ns = int(time.time() * 1_000_000_000)
        start_ns = now_ns - (10 * 60 * 1_000_000_000)
        query = '{job=~".+"}'
        query_path = (
            f"/loki/api/v1/query_range?query={quote(query, safe='')}"
            f"&limit=1&start={start_ns}&end={now_ns}&direction=backward"
        )
        rc, body = _loki_get(query_path, timeout=15)
        ingest_ok = False
        ingest_detail = "query_range unreachable"
        if rc == 0 and body:
            try:
                streams = json.loads(body).get("data", {}).get("result", [])
                ingest_ok = len(streams) > 0
                ingest_detail = f"{len(streams)} stream(s) in last 10m" if ingest_ok else "no log streams in last 10m"
            except json.JSONDecodeError:
                ingest_detail = "invalid query_range JSON"
        checks.append(VerifyCheck("loki", "log_ingest", ingest_ok, ingest_detail))

        return checks
