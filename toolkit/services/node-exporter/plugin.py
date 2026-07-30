"""Node Exporter service plugin with host-metric verification."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class NodeExporterPlugin(ServicePlugin):
    service = "node-exporter"
    category = "management"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require host identity, filesystem, and CPU metrics on every managed node."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        checks: list[VerifyCheck] = []
        nodes = cfg.enabled_nodes if cfg.is_multi_node else ("local",)
        for node in nodes:
            address = cfg.node_ip(node) if cfg.is_multi_node else vm_ip
            container = (
                "node-exporter" if not cfg.is_multi_node or node == self.runtime_node(cfg) else "node-exporter-agent"
            )
            if not container_exists_on_vm(cfg, address, container, root):
                checks.append(VerifyCheck("node-exporter", f"{node}_host_metrics", False, f"{container} missing"))
                continue
            rc, body = docker_curl(cfg, address, container, "http://localhost:9100/metrics", root=root, timeout=15)
            families = {
                "identity": "node_uname_info{" in (body or ""),
                "filesystem": "node_filesystem_avail_bytes{" in (body or ""),
                "cpu": "node_cpu_seconds_total{" in (body or ""),
            }
            missing = [name for name, present in families.items() if not present]
            checks.append(
                VerifyCheck(
                    "node-exporter",
                    f"{node}_host_metrics",
                    rc == 0 and not missing,
                    (
                        "identity, filesystem, and CPU metrics present"
                        if not missing
                        else f"missing: {', '.join(missing)}"
                    ),
                )
            )
        return checks
