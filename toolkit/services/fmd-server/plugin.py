"""Find My Device service verification."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class FmdServerPlugin(ServicePlugin):
    service = "fmd-server"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        if not container_exists_on_vm(cfg, vm_ip, self.service, root):
            return [VerifyCheck(self.service, "deployment", False, "container is missing")]

        version_rc, version = docker_curl(
            cfg,
            vm_ip,
            self.service,
            "http://localhost:8080/api/v1/version",
            root=root,
        )
        version_ok = version_rc == 0 and version.strip() == "0.16.0"

        metrics_rc, metrics = docker_curl(
            cfg,
            vm_ip,
            self.service,
            "http://localhost:9100/metrics",
            root=root,
        )
        metrics_ok = metrics_rc == 0 and "fmd_accounts" in metrics

        return [
            VerifyCheck(
                self.service,
                "api_version",
                version_ok,
                version.strip() if version_ok else "expected FMD Server 0.16.0",
            ),
            VerifyCheck(
                self.service,
                "metrics",
                metrics_ok,
                "Prometheus metrics available" if metrics_ok else "metrics endpoint unavailable",
            ),
        ]
