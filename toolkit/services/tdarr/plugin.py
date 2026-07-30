"""tdarr service plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import RuntimeEnvironmentContext, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


def _check_tdarr_status(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Tdarr server status API must respond."""
    import httpx
    from toolkit.core.manifest.settings import service_enabled
    from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

    if not service_enabled(cfg, "tdarr"):
        return VerifyCheck("tdarr", "server", True, "Tdarr not enabled — skip")
    if not container_exists_on_vm(cfg, vm_ip, "tdarr", root):
        return VerifyCheck("tdarr", "server", False, "container missing")
    if cfg.is_multi_node:
        rc, body = docker_curl(cfg, vm_ip, "tdarr", "http://localhost:8266/api/v2/status", root=root)
        if rc != 0 or not body:
            return VerifyCheck("tdarr", "server", False, "status API unreachable")
        return VerifyCheck("tdarr", "server", True, "status ok")
    try:
        resp = httpx.get("http://localhost:8266/api/v2/status", timeout=10)
        return VerifyCheck("tdarr", "server", resp.status_code == 200, f"HTTP {resp.status_code}")
    except httpx.HTTPError:
        return VerifyCheck("tdarr", "server", False, "API unreachable")


def _check_tdarr_nodes(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """At least one Tdarr node registered."""
    import httpx
    from toolkit.core.manifest.settings import service_enabled
    from toolkit.services.sdk import VerifyCheck, docker_curl

    if not service_enabled(cfg, "tdarr"):
        return VerifyCheck("tdarr", "nodes", True, "Tdarr not enabled — skip")
    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "tdarr",
            "http://localhost:8265/api/v2/cruddb",
            root=root,
            method="POST",
            body=json.dumps({"data": {"collection": "NodeJSONDB", "mode": "getAll"}}),
            headers={"Content-Type": "application/json"},
        )
        if rc != 0 or not body:
            return VerifyCheck("tdarr", "nodes", False, "nodes API unreachable")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return VerifyCheck("tdarr", "nodes", False, "invalid nodes JSON")
    else:
        try:
            resp = httpx.post(
                "http://localhost:8265/api/v2/cruddb",
                json={"data": {"collection": "NodeJSONDB", "mode": "getAll"}},
                timeout=10,
            )
            if resp.status_code != 200:
                return VerifyCheck("tdarr", "nodes", False, f"HTTP {resp.status_code}")
            data = resp.json()
        except httpx.HTTPError:
            return VerifyCheck("tdarr", "nodes", False, "API unreachable")

    if isinstance(data, list):
        node_count = len(data)
    elif isinstance(data, dict):
        node_count = len(data.get("nodes") or data.get("array") or [])
    else:
        node_count = 0
    return VerifyCheck(
        "tdarr",
        "nodes",
        node_count >= 1,
        f"{node_count} node(s)" if node_count else "no nodes registered",
    )


def _check_tdarr_flows(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Probe the Tdarr transcode-pipeline API and report configured flow count."""
    import httpx
    from toolkit.core.manifest.settings import service_enabled
    from toolkit.services.sdk import VerifyCheck, docker_curl

    if not service_enabled(cfg, "tdarr"):
        return VerifyCheck("tdarr", "flows", True, "Tdarr not enabled — skip")
    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "tdarr",
            "http://localhost:8265/api/v2/cruddb",
            root=root,
            method="POST",
            body=json.dumps({"data": {"collection": "FlowsJSONDB", "mode": "getAll"}}),
            headers={"Content-Type": "application/json"},
        )
        if rc != 0 or not body:
            return VerifyCheck("tdarr", "flows", False, "pipelines endpoint unavailable")
        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            return VerifyCheck("tdarr", "flows", False, "pipelines API returned invalid JSON")
    else:
        try:
            resp = httpx.post(
                "http://localhost:8265/api/v2/cruddb",
                json={"data": {"collection": "FlowsJSONDB", "mode": "getAll"}},
                timeout=10,
            )
            if resp.status_code != 200:
                return VerifyCheck("tdarr", "flows", False, f"pipelines HTTP {resp.status_code}")
            data = resp.json()
        except httpx.HTTPError:
            return VerifyCheck("tdarr", "flows", False, "API unreachable")
    if isinstance(data, dict):
        flow_count = len(data.get("pipelines") or data.get("flows") or data.get("nodes") or [])
    elif isinstance(data, list):
        flow_count = len(data)
    else:
        flow_count = 0
    detail = f"{flow_count} flow(s) configured" if flow_count > 0 else "no flows found"
    return VerifyCheck("tdarr", "flows", flow_count > 0, detail)


def _check_tdarr_flow_assets(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Require the current Tdarr flow-template API to expose community assets."""
    import httpx
    from toolkit.core.manifest.settings import service_enabled
    from toolkit.services.sdk import VerifyCheck, docker_curl

    if not service_enabled(cfg, "tdarr"):
        return VerifyCheck("tdarr", "flow_assets", True, "Tdarr not enabled — skip")
    payload = json.dumps({"data": {"string": "", "pluginType": "Community"}})
    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "tdarr",
            "http://localhost:8265/api/v2/search-flow-templates",
            root=root,
            method="POST",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        if rc != 0 or not body:
            return VerifyCheck("tdarr", "flow_assets", False, "flow-template API unreachable")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return VerifyCheck("tdarr", "flow_assets", False, "flow-template API returned invalid JSON")
    else:
        try:
            response = httpx.post(
                "http://localhost:8265/api/v2/search-flow-templates",
                json={"data": {"string": "", "pluginType": "Community"}},
                timeout=10,
            )
            if response.status_code != 200:
                return VerifyCheck("tdarr", "flow_assets", False, f"flow-template HTTP {response.status_code}")
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return VerifyCheck("tdarr", "flow_assets", False, "flow-template API unreachable")
    templates = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    return VerifyCheck(
        "tdarr",
        "flow_assets",
        bool(templates),
        f"{len(templates)} community flow template(s)" if templates else "no community flow templates",
    )


class TdarrPlugin(ServicePlugin):
    service = "tdarr"
    category = "media"

    def runtime_environment(self, context: RuntimeEnvironmentContext) -> dict[str, str]:
        """Resolve worker counts from desired settings and detected node capacity."""
        from toolkit.core.manifest.settings import service_setting_int
        from toolkit.services.tdarr.capabilities import resolve_cpu_workers, resolve_gpu_workers

        return {
            "TDARR_CPU_WORKERS": str(resolve_cpu_workers(context.config, root=context.root)),
            "TDARR_GPU_WORKERS": str(resolve_gpu_workers(context.config, root=context.root)),
            "TDARR_HEALTH_CPU_WORKERS": str(service_setting_int(context.config, self.service, "health-cpu-workers")),
        }

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Reconcile Tdarr plugins, flows, libraries, and cache coordination."""
        from toolkit.core.manifest.settings import service_enabled

        if not service_enabled(cfg, self.service):
            return []
        from toolkit.services.tdarr.bootstrap import configure_tdarr

        try:
            return configure_tdarr(cfg, root=root)
        except Exception as exc:
            return [f"WARNING: Tdarr setup failed: {exc}"]

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Tdarr server status, node count, and transcode pipeline configuration."""
        return [
            _check_tdarr_status(cfg, vm_ip, root),
            _check_tdarr_nodes(cfg, vm_ip, root),
            _check_tdarr_flows(cfg, vm_ip, root),
            _check_tdarr_flow_assets(cfg, vm_ip, root),
        ]
