"""rclone service plugin.

Owns verify() for the media-cache rclone mount (container health + /library
mountpoint) on top of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class RclonePlugin(ServicePlugin):
    service = "rclone"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.core.manifest.settings import service_enabled
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_exec_on_vm,
            docker_health_status_on_vm,
        )

        if not service_enabled(cfg, "media-cache"):
            return [VerifyCheck("rclone", "mount", True, "skipped (media cache disabled)")]

        if not container_exists_on_vm(cfg, vm_ip, "rclone", root):
            return [
                VerifyCheck("rclone", "container", False, "container missing"),
                VerifyCheck("rclone", "mount", False, "container missing"),
            ]

        state, health = docker_health_status_on_vm(cfg, vm_ip, "rclone", root)
        running = state == "running"
        container_ok = running and health in ("healthy", "starting", "")
        container_detail = f"state={state or 'unknown'}" + (f", health={health}" if health else "")
        checks: list[VerifyCheck] = [
            VerifyCheck("rclone", "container", container_ok, container_detail),
        ]

        rc, out = docker_exec_on_vm(cfg, "rclone", ["mountpoint", "-q", "/library"], vm_ip, root)
        mount_ok = rc == 0
        mount_detail = "/library mounted" if mount_ok else (out or "mountpoint check failed")[:80]
        checks.append(VerifyCheck("rclone", "mount", mount_ok, mount_detail))

        # compose.yaml runs `rclone mount` only — no RC API is exposed.
        checks.append(VerifyCheck("rclone", "rc_api", True, "skipped (RC not configured in compose)"))
        return checks
