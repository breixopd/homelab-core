"""homelab-ui service plugin — defaults from service.yaml; override post_start/verify/heal when needed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class HomelabUiPlugin(ServicePlugin):
    service = "homelab-ui"
    category = "management"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Lightweight reachability when deployed; honest skip otherwise."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        if not cfg.category_enabled("management"):
            return [VerifyCheck("homelab-ui", "ui", True, "management not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("homelab-ui", "ui", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "homelab-ui", root):
            return [VerifyCheck("homelab-ui", "ui", False, "container missing")]

        rc, body = docker_curl(cfg, vm_ip, "homelab-ui", "http://localhost:8080/", root=root, timeout=10)
        ok = rc == 0
        return [VerifyCheck("homelab-ui", "ui", ok, "UI reachable" if ok else (body or "unreachable")[:80])]
