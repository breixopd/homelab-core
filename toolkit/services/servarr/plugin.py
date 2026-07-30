"""Servarr integration plugin with cross-application verification."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


def _raise_on_wiring_failure(logs: list[str], *, operation: str) -> None:
    for line in logs:
        lower = line.lower()
        if "failed to register" in lower or "could not" in lower:
            raise RuntimeError(f"{operation}: {line}")
        if lower.endswith("failed") or ": failed" in lower:
            raise RuntimeError(f"{operation}: {line}")


class ServarrPlugin(ServicePlugin):
    service = "servarr"
    category = "media"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Reconcile APIs, download clients, indexers, requests, subtitles, and alerts."""
        import importlib

        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password
        from toolkit.core.secrets.secrets import merge_secret_values
        from toolkit.services._arr import (
            configure_arr_download_client,
            configure_arr_root_folder,
            extract_bazarr_api_key,
            reconcile_servarr_api_key,
            wait_for_arr_api,
            wire_arr_notifications,
            wire_bazarr_arr,
            wire_bazarr_providers,
            wire_prowlarr_apps,
            wire_seerr_arr,
        )

        if root is None:
            raise RuntimeError("Servarr integration requires the installation root")
        logs: list[str] = []

        extract_seerr_api_key = importlib.import_module("toolkit.services.seerr.bootstrap").extract_seerr_api_key
        extracted_seerr_key = extract_seerr_api_key(root)
        if extracted_seerr_key and extracted_seerr_key != secrets.get("SEERR_API_KEY"):
            logs.extend(merge_secret_values(root, {"SEERR_API_KEY": extracted_seerr_key}))
            secrets["SEERR_API_KEY"] = extracted_seerr_key

        prowlarr_key = reconcile_servarr_api_key(root, "prowlarr", secrets, "PROWLARR_API_KEY")
        sonarr_key = reconcile_servarr_api_key(root, "sonarr", secrets, "SONARR_API_KEY")
        radarr_key = reconcile_servarr_api_key(root, "radarr", secrets, "RADARR_API_KEY")
        prowlarr_url = resolve_docker_service_url("prowlarr", 9696)
        sonarr_url = resolve_docker_service_url("sonarr", 8989)
        radarr_url = resolve_docker_service_url("radarr", 7878)

        for name, url, api_key, version in (
            ("Prowlarr", prowlarr_url, prowlarr_key, "v1"),
            ("Sonarr", sonarr_url, sonarr_key, "v3"),
            ("Radarr", radarr_url, radarr_key, "v3"),
        ):
            if api_key and not wait_for_arr_api(url, api_key, timeout=45, api_version=version):
                raise RuntimeError(f"{name}: API not ready after wait")

        if prowlarr_key and sonarr_key and radarr_key:
            integration_logs = wire_prowlarr_apps(
                prowlarr_url=prowlarr_url,
                prowlarr_api_key=prowlarr_key,
                sonarr_url="http://sonarr:8989",
                sonarr_api_key=sonarr_key,
                radarr_url="http://radarr:7878",
                radarr_api_key=radarr_key,
            )
            logs.extend(integration_logs)
            _raise_on_wiring_failure(integration_logs, operation="Prowlarr auto-wire")

        from toolkit.core.manifest.settings import service_enabled, service_setting_bool

        vpn_enabled = service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")
        qbit_host = "gluetun" if vpn_enabled else "qbittorrent"
        qbit_user = (secrets.get("QBITTORRENT_USER") or "admin").strip() or "admin"
        qbit_password = secrets.get("QBITTORRENT_PASSWORD", "")
        for name, url, api_key, folder, category in (
            ("Sonarr", sonarr_url, sonarr_key, "/data/tv", "tv-sonarr"),
            ("Radarr", radarr_url, radarr_key, "/data/movies", "radarr"),
        ):
            if not api_key:
                continue
            if not configure_arr_root_folder(url, api_key, folder):
                raise RuntimeError(f"{name} root folder configuration failed for {folder}")
            if not configure_arr_download_client(
                url,
                api_key,
                qbit_host=qbit_host,
                qbit_user=qbit_user,
                qbit_password=qbit_password,
                category=category,
            ):
                raise RuntimeError(f"{name} download client configuration failed")

        from toolkit.services import get_service_plugin

        bazarr = get_service_plugin("bazarr")
        bazarr_source = (
            bazarr.manifest.host_sources["BAZARR_CONFIG_SOURCE"].path if bazarr is not None else "data/bazarr/config"
        )
        bazarr_key = extract_bazarr_api_key(root / bazarr_source / "config" / "config.yaml") or ""
        if not bazarr_key:
            raise RuntimeError("Bazarr API key is unavailable after startup")
        if bazarr_key and sonarr_key and radarr_key:
            bazarr_logs = wire_bazarr_arr(
                resolve_docker_service_url("bazarr", 6767),
                bazarr_key,
                "http://sonarr:8989",
                sonarr_key,
                "http://radarr:7878",
                radarr_key,
            )
            logs.extend(bazarr_logs)
            _raise_on_wiring_failure(bazarr_logs, operation="Bazarr auto-wire")
        provider_logs = wire_bazarr_providers(
            resolve_docker_service_url("bazarr", 6767),
            bazarr_key,
            opensubtitles_user=secrets.get("OPENSUBTITLES_USER", ""),
            opensubtitles_password=secrets.get("OPENSUBTITLES_PASSWORD", ""),
            flaresolverr_url="http://flaresolverr:8191/v1",
        )
        logs.extend(provider_logs)
        _raise_on_wiring_failure(provider_logs, operation="Bazarr provider setup")

        seerr_key = secrets.get("SEERR_API_KEY", "")
        if seerr_key and sonarr_key and radarr_key:
            from toolkit.core.manifest.settings import service_setting_str

            wants_jellyfin = service_setting_str(cfg, "media-library", "server") in ("jellyfin", "both")
            seerr_logs = wire_seerr_arr(
                resolve_docker_service_url("seerr", 5055),
                seerr_key,
                "http://sonarr:8989",
                sonarr_key,
                "http://radarr:7878",
                radarr_key,
                jellyfin_url="http://jellyfin:8096" if wants_jellyfin else "",
                jellyfin_api_key=secrets.get("JELLYFIN_API_KEY", "") if wants_jellyfin else "",
                jellyfin_user="admin" if wants_jellyfin else "",
                jellyfin_password=(
                    resolve_bootstrap_password(secrets, "JELLYFIN_ADMIN_PASSWORD") if wants_jellyfin else ""
                ),
                plex_token=secrets.get("PLEX_TOKEN", ""),
            )
            logs.extend(seerr_logs)
            _raise_on_wiring_failure(seerr_logs, operation="Seerr auto-wire")

        if cfg.category_enabled("notifications"):
            from toolkit.services.ntfy.client import resolve_infra_ntfy_url

            ntfy_url = resolve_infra_ntfy_url(cfg)
            for name, url, api_key, topic in (
                ("Sonarr", sonarr_url, sonarr_key, "arr-sonarr"),
                ("Radarr", radarr_url, radarr_key, "arr-radarr"),
            ):
                if not api_key:
                    continue
                if wire_arr_notifications(url, api_key, ntfy_url, topic):
                    logs.append(f"{name}: ntfy notifications configured")
                else:
                    logs.append(f"WARNING: {name} ntfy wiring failed")
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Aggregate the functional links that make the media automation stack work end to end."""
        from toolkit.services._arr import (
            verify_arr_standard,
            verify_bazarr_standard,
            verify_prowlarr_standard,
            verify_seerr_standard,
        )
        from toolkit.services.sdk import VerifyCheck

        prowlarr_key = secrets.get("PROWLARR_API_KEY", "")
        radarr_key = secrets.get("RADARR_API_KEY", "")
        sonarr_key = secrets.get("SONARR_API_KEY", "")
        if not all((prowlarr_key, radarr_key, sonarr_key)):
            return [VerifyCheck(self.service, "stack_contracts", False, "one or more Servarr API keys are missing")]

        required = {
            "bazarr": {"arr_links", "languages", "providers"},
            "prowlarr": {"applications", "indexers"},
            "radarr": {"download_client", "download_client_test", "indexers", "root_folders"},
            "seerr": {"connections"},
            "sonarr": {"download_client", "download_client_test", "indexers", "root_folders"},
        }
        suites = (
            verify_bazarr_standard(cfg, secrets, vm_ip, root),
            verify_prowlarr_standard(cfg, "http://prowlarr:9696", "prowlarr", vm_ip, root, prowlarr_key),
            verify_arr_standard("radarr", cfg, "http://radarr:7878", "radarr", 7878, vm_ip, root, radarr_key),
            verify_seerr_standard(cfg, secrets, vm_ip, root),
            verify_arr_standard("sonarr", cfg, "http://sonarr:8989", "sonarr", 8989, vm_ip, root, sonarr_key),
        )
        observed = [check for suite in suites for check in suite if check.check in required.get(check.service, set())]
        expected_count = sum(len(checks) for checks in required.values())
        failures = [f"{check.service}.{check.check}" for check in observed if not check.passed]
        passed = len(observed) == expected_count and not failures
        detail = (
            f"{expected_count} cross-service contracts passed"
            if passed
            else f"failed or missing: {', '.join(failures) or f'{len(observed)}/{expected_count} observed'}"
        )
        return [VerifyCheck(self.service, "stack_contracts", passed, detail)]
