"""RomM service verification and management status."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin


@dataclass(frozen=True)
class _ProviderSpec:
    slug: str
    label: str
    setting: str | None = None
    required_secrets: tuple[str, ...] = ()


_METADATA_PROVIDERS = {
    "HASHEOUS_API_ENABLED": _ProviderSpec("hasheous", "Hasheous", setting="hasheous-enabled"),
    "HLTB_API_ENABLED": _ProviderSpec("hltb", "HowLongToBeat", setting="hltb-enabled"),
    "TGDB_API_ENABLED": _ProviderSpec("tgdb", "TheGamesDB", setting="tgdb-enabled"),
    "PLAYMATCH_API_ENABLED": _ProviderSpec("playmatch", "PlayMatch", setting="playmatch-enabled"),
    "FLASHPOINT_API_ENABLED": _ProviderSpec("flashpoint", "Flashpoint", setting="flashpoint-enabled"),
    "IGDB_API_ENABLED": _ProviderSpec(
        "igdb",
        "IGDB",
        required_secrets=("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET"),
    ),
    "SS_API_ENABLED": _ProviderSpec(
        "screenscraper",
        "ScreenScraper",
        required_secrets=("SCREENSCRAPER_USER", "SCREENSCRAPER_PASSWORD"),
    ),
    "STEAMGRIDDB_API_ENABLED": _ProviderSpec(
        "steamgriddb",
        "SteamGridDB",
        required_secrets=("STEAMGRIDDB_API_KEY",),
    ),
    "RA_API_ENABLED": _ProviderSpec(
        "retroachievements",
        "RetroAchievements",
        required_secrets=("RETROACHIEVEMENTS_API_KEY",),
    ),
    "MOBY_API_ENABLED": _ProviderSpec("moby", "MobyGames"),
    "LAUNCHBOX_API_ENABLED": _ProviderSpec("launchbox", "LaunchBox"),
    "LIBRETRO_API_ENABLED": _ProviderSpec("libretro", "Libretro"),
}

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class RommPlugin(ServicePlugin):
    service = "romm"
    category = "cloud"

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        from toolkit.core.manifest.settings import service_setting_bool
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
                if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                    result[key] = value
            sources = payload.get("METADATA_SOURCES")
            if isinstance(sources, dict):
                configured = 0
                observed = 0
                aligned = 0
                for source_key, spec in _METADATA_PROVIDERS.items():
                    configured_value: bool | None
                    if spec.setting:
                        try:
                            configured_value = service_setting_bool(cfg, self.service, spec.setting)
                        except (TypeError, ValueError):
                            configured_value = None
                    elif spec.required_secrets:
                        configured_value = all(bool(secrets.get(name)) for name in spec.required_secrets)
                    else:
                        configured_value = None
                    if configured_value is not None:
                        configured += configured_value
                        result[f"metadata_{spec.slug}_configured"] = int(configured_value)
                    source_value = sources.get(source_key)
                    if isinstance(source_value, bool):
                        observed += source_value
                        aligned += configured_value is None or source_value == configured_value
                        result[f"metadata_{spec.slug}_enabled"] = int(source_value)
                result["metadata_providers_configured"] = configured
                result["metadata_providers_enabled"] = observed
                result["metadata_providers_aligned"] = aligned
                result["metadata_sources_observed"] = 1
        return result

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        """Expose provider configuration and observed runtime state without secrets."""
        status = self.status(cfg, secrets, root)
        if not status or status.get("metadata_sources_observed") != 1:
            raise RuntimeError("RomM metadata provider status is unavailable")
        providers: list[dict[str, object]] = []
        for spec in _METADATA_PROVIDERS.values():
            configured_value = status.get(f"metadata_{spec.slug}_configured")
            configured = configured_value == 1
            observed = status.get(f"metadata_{spec.slug}_enabled")
            runtime = "Unknown" if observed is None else ("Enabled" if observed == 1 else "Disabled")
            if configured_value is None:
                configuration = "Automatic"
                parity = "Enabled" if observed == 1 else ("Disabled" if observed == 0 else "Unknown")
            elif not configured:
                configuration = "No"
                parity = "Aligned" if observed == 0 else ("Drift" if observed == 1 else "Unknown")
            else:
                configuration = "Yes"
                parity = "Aligned" if observed == 1 else ("Drift" if observed == 0 else "Unknown")
            providers.append(
                {
                    "provider": spec.label,
                    "configured": configuration,
                    "runtime": runtime,
                    "config_parity": parity,
                }
            )
        return {"metadata_providers": providers}

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
