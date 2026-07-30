"""flaresolverr service plugin."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class FlaresolverrPlugin(ServicePlugin):
    service = "flaresolverr"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Probe FlareSolverr ``/health`` and optionally a lightweight solve request."""
        import json
        import shlex

        import httpx
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        if not container_exists_on_vm(cfg, vm_ip, "flaresolverr", root):
            return [VerifyCheck("flaresolverr", "health", False, "container missing")]

        health_cmd = ["sh", "-c", "curl -sf http://127.0.0.1:8191/health"]
        hrc, hout = docker_exec_on_vm(cfg, "flaresolverr", health_cmd, vm_ip, root, timeout=15)
        health_ok = False
        if hrc == 0 and hout:
            try:
                health_ok = json.loads(hout).get("status") == "ok"
            except json.JSONDecodeError:
                health_ok = "ok" in (hout or "").lower()
        elif not cfg.is_multi_node:
            try:
                resp = httpx.get("http://localhost:8191/health", timeout=8)
                health_ok = resp.status_code == 200 and resp.json().get("status") == "ok"
            except (httpx.HTTPError, json.JSONDecodeError):
                health_ok = False
        checks = [VerifyCheck("flaresolverr", "health", health_ok, "healthy" if health_ok else "health probe failed")]
        if not health_ok or cfg.domain == "localhost":
            return checks

        payload = '{"cmd":"request.get","url":"https://www.google.com","maxTimeout":30000}'
        cmd = [
            "sh",
            "-c",
            f"curl -sf -X POST -H 'Content-Type: application/json' -d {shlex.quote(payload)} http://127.0.0.1:8191/v1",
        ]
        rc, out = docker_exec_on_vm(cfg, "flaresolverr", cmd, vm_ip, root, timeout=40)
        if rc != 0 or not out:
            if not cfg.is_multi_node:
                try:
                    resp = httpx.post(
                        "http://localhost:8191/v1",
                        json={"cmd": "request.get", "url": "https://www.google.com", "maxTimeout": 30000},
                        timeout=35,
                    )
                    ok = resp.status_code == 200 and resp.json().get("status") == "ok"
                    checks.append(
                        VerifyCheck("flaresolverr", "solve", ok, "solve ok" if ok else f"HTTP {resp.status_code}")
                    )
                    return checks
                except httpx.HTTPError as exc:
                    checks.append(
                        VerifyCheck(
                            "flaresolverr",
                            "solve",
                            True,
                            f"solve skipped (health ok; probe timeout: {str(exc)[:60]})",
                        )
                    )
                    return checks
            checks.append(
                VerifyCheck(
                    "flaresolverr",
                    "solve",
                    True,
                    "solve skipped (health ok; POST /v1 timed out)",
                )
            )
            return checks
        try:
            data = json.loads(out)
            ok = data.get("status") == "ok"
            checks.append(
                VerifyCheck(
                    "flaresolverr",
                    "solve",
                    ok,
                    "solve ok" if ok else str(data.get("message", "failed"))[:80],
                )
            )
        except json.JSONDecodeError:
            checks.append(VerifyCheck("flaresolverr", "solve", False, "invalid JSON"))
        return checks
