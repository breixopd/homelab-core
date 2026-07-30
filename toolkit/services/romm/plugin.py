"""RomM service verification and management status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class RommPlugin(ServicePlugin):
    service = "romm"
    category = "cloud"

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        from toolkit.services.sdk import docker_curl

        vm_ip = self.runtime_address(cfg)
        rc, body = docker_curl(cfg, vm_ip, self.service, "http://localhost:8080/api/heartbeat", root=root, timeout=10)
        if rc != 0:
            return {}
        result: dict[str, object] = {"heartbeat": 1}
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return result
        if isinstance(payload, dict):
            for key in ("roms", "platforms", "users"):
                value = payload.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    result[key] = value
            sources = payload.get("METADATA_SOURCES")
            if isinstance(sources, dict):
                result["metadata_providers_enabled"] = sum(
                    value is True for key, value in sources.items() if key.endswith("_API_ENABLED")
                )
        return result

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl, docker_exec_on_vm

        if not container_exists_on_vm(cfg, vm_ip, self.service, root):
            return [VerifyCheck(self.service, "deployment", False, "container is missing")]
        rc, body = docker_curl(
            cfg,
            vm_ip,
            self.service,
            "http://localhost:8080/api/heartbeat",
            root=root,
            timeout=15,
        )
        healthy = rc == 0 and bool((body or "").strip())
        checks = [
            VerifyCheck(self.service, "heartbeat", healthy, "heartbeat ok" if healthy else "heartbeat unreachable"),
        ]
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            payload = {}
        wizard = None
        if isinstance(payload, dict):
            system = payload.get("SYSTEM")
            if isinstance(system, dict):
                wizard = system.get("SHOW_SETUP_WIZARD")
            if wizard is None:
                wizard = payload.get("show_setup_wizard")
        checks.append(
            VerifyCheck(
                self.service,
                "setup_wizard",
                wizard is False,
                "setup wizard disabled" if wizard is False else "setup wizard state unavailable or enabled",
            )
        )
        from toolkit.core.manifest.settings import service_setting_bool

        sources = payload.get("METADATA_SOURCES") if isinstance(payload, dict) else None
        provider_settings = {
            "HASHEOUS_API_ENABLED": "hasheous-enabled",
            "HLTB_API_ENABLED": "hltb-enabled",
            "TGDB_API_ENABLED": "tgdb-enabled",
            "PLAYMATCH_API_ENABLED": "playmatch-enabled",
            "FLASHPOINT_API_ENABLED": "flashpoint-enabled",
        }
        provider_drift = [
            provider
            for provider, setting in provider_settings.items()
            if not isinstance(sources, dict)
            or sources.get(provider) is not service_setting_bool(cfg, self.service, setting)
        ]
        checks.append(
            VerifyCheck(
                self.service,
                "metadata_providers",
                not provider_drift,
                "configured metadata providers active"
                if not provider_drift
                else "provider configuration drift: " + ", ".join(provider_drift),
            )
        )
        rc2, out = docker_exec_on_vm(
            cfg,
            self.service,
            ["sh", "-c", "test -d /romm/library/roms && test -w /romm/library/roms"],
            vm_ip,
            root,
            timeout=15,
        )
        checks.append(
            VerifyCheck(
                self.service,
                "library",
                rc2 == 0,
                "library/roms writable" if rc2 == 0 else (out or "library/roms missing or not writable")[:120],
            )
        )
        return checks
