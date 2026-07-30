"""Alloy service plugin with end-to-end log collection verification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


_RUNNING_COMPONENTS = re.compile(
    r'^alloy_component_controller_running_components\{[^}]*health_type="healthy"[^}]*\}\s+([0-9.eE+-]+)$',
    re.MULTILINE,
)
_COLLECTED_ENTRIES = re.compile(r"^loki_source_docker_target_entries_total\{[^}]*\}\s+([0-9.eE+-]+)$", re.MULTILINE)


class AlloyPlugin(ServicePlugin):
    service = "alloy"
    category = "management"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require healthy components and evidence that Docker logs were collected on every managed node."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        checks: list[VerifyCheck] = []
        nodes = cfg.enabled_nodes if cfg.is_multi_node else ("local",)
        for node in nodes:
            address = cfg.node_ip(node) if cfg.is_multi_node else vm_ip
            container = "alloy" if not cfg.is_multi_node or node == self.runtime_node(cfg) else "alloy-agent"
            if not container_exists_on_vm(cfg, address, container, root):
                checks.append(VerifyCheck("alloy", f"{node}_pipeline", False, f"{container} missing"))
                continue
            rc, body = docker_curl(
                cfg,
                address,
                container,
                "http://localhost:12345/metrics",
                root=root,
                timeout=15,
            )
            component_values = [float(value) for value in _RUNNING_COMPONENTS.findall(body or "")]
            entry_values = [float(value) for value in _COLLECTED_ENTRIES.findall(body or "")]
            components = int(sum(component_values))
            entries = int(sum(entry_values))
            passed = rc == 0 and components > 0 and entries > 0
            checks.append(
                VerifyCheck(
                    "alloy",
                    f"{node}_pipeline",
                    passed,
                    (
                        f"{components} healthy components; {entries} Docker log entries collected"
                        if passed
                        else f"components={components}, collected_entries={entries}"
                    ),
                )
            )
        return checks
