"""cAdvisor service plugin with semantic metrics verification."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class CadvisorPlugin(ServicePlugin):
    service = "cadvisor"
    category = "management"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require fresh container inventory and CPU metrics on every managed node."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        checks: list[VerifyCheck] = []
        nodes = cfg.enabled_nodes if cfg.is_multi_node else ("local",)
        for node in nodes:
            address = cfg.node_ip(node) if cfg.is_multi_node else vm_ip
            container = "cadvisor" if not cfg.is_multi_node or node == self.runtime_node(cfg) else "cadvisor-agent"
            if not container_exists_on_vm(cfg, address, container, root):
                checks.append(VerifyCheck("cadvisor", f"{node}_metrics", False, f"{container} missing"))
                continue
            rc, body = docker_curl(cfg, address, container, "http://localhost:8080/metrics", root=root, timeout=15)
            inventory = sum(1 for line in (body or "").splitlines() if line.startswith("container_last_seen{"))
            cpu = "container_cpu_usage_seconds_total{" in (body or "")
            passed = rc == 0 and inventory > 0 and cpu
            checks.append(
                VerifyCheck(
                    "cadvisor",
                    f"{node}_metrics",
                    passed,
                    f"{inventory} containers reporting CPU metrics" if passed else "container metrics incomplete",
                )
            )
        return checks
