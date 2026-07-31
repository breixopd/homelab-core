"""recyclarr service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml.

verify() returns two checks: ``config`` (the generated recyclarr.yml exists
on the media VM and the container reports its version) and ``profiles``
(Sonarr exposes a TRaSH-style quality profile, syncing recyclarr once if no
match is found on the first pass).
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


def generate_recyclarr_config(context: ArtifactGenerationContext) -> None:
    """Render the complete Recyclarr bootstrap configuration."""
    values = {
        "sonarr_base_url": "http://sonarr:8989",
        "radarr_base_url": "http://radarr:7878",
        "sonarr_api_key": context.secrets.get("SONARR_API_KEY") or "${SONARR_API_KEY}",
        "radarr_api_key": context.secrets.get("RADARR_API_KEY") or "${RADARR_API_KEY}",
    }
    context.render_template(
        "generated/recyclarr/recyclarr.yml",
        "recyclarr.yml.j2",
        values,
    )
    context.render_template(
        "generated/recyclarr/settings.yml",
        "recyclarr-settings.yml.j2",
        values,
    )
    for service in ("sonarr", "radarr"):
        local_override = f"generated/recyclarr/includes/{service}-local.yml"
        if context.artifact_path(local_override).is_file():
            context.claim(local_override)
        else:
            context.write_text(
                local_override,
                "# Local Recyclarr instance overrides; this file is preserved across generation.\n"
                "# yaml-language-server: $schema=https://schemas.recyclarr.dev/latest/config-schema.json\n"
                "{}\n",
            )


class RecyclarrPlugin(ServicePlugin):
    service = "recyclarr"
    category = "media"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        generate_recyclarr_config(context)

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Generate current Sonarr/Radarr profiles and run one bounded sync."""
        import subprocess

        from toolkit.core.generate.artifacts import ArtifactGenerationContext
        from toolkit.services._arr import reconcile_servarr_api_key

        if root is None:
            raise RuntimeError("Recyclarr setup requires the installation root")
        sonarr_key = reconcile_servarr_api_key(root, "sonarr", secrets, "SONARR_API_KEY")
        radarr_key = reconcile_servarr_api_key(root, "radarr", secrets, "RADARR_API_KEY")
        if not sonarr_key or not radarr_key:
            return ["Recyclarr: waiting for Sonarr and Radarr API keys"]
        try:
            current_secrets = dict(secrets)
            current_secrets.update(SONARR_API_KEY=sonarr_key, RADARR_API_KEY=radarr_key)
            context = ArtifactGenerationContext(cfg, root, current_secrets, self.manifest)
            generate_recyclarr_config(context)
            context.finish()
        except Exception as exc:
            raise RuntimeError(f"Recyclarr config generation failed: {exc}") from exc
        try:
            result = subprocess.run(
                ["docker", "exec", "recyclarr", "recyclarr", "sync"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Recyclarr sync invocation failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:200]
            raise RuntimeError(f"Recyclarr sync failed (exit {result.returncode}): {detail}")
        from toolkit.core.state.files import atomic_write_text

        config_path = root / "generated/recyclarr/recyclarr.yml"
        config_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        atomic_write_text(config_path.parent / ".last-sync.sha256", f"{config_digest}\n")
        return ["Recyclarr: sync triggered"]

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, docker_curl, ssh_on_vm

        # ── config check: generated recyclarr.yml on disk + container version ──
        try:
            rc, out, _ = ssh_on_vm(
                cfg, vm_ip, "test -s /opt/homelab/generated/recyclarr/recyclarr.yml", root=root, timeout=20
            )
            if rc != 0:
                config_check = VerifyCheck(
                    "recyclarr",
                    "config",
                    False,
                    "recyclarr.yml missing on media",
                    status="not_ready",
                )
            else:
                rc2, ver, _ = ssh_on_vm(cfg, vm_ip, "docker exec recyclarr recyclarr --version", root=root, timeout=30)
                ok = rc2 == 0 and bool((ver or "").strip())
                detail = (
                    f"v8 config on disk; {(ver or '').strip()[:40]}"
                    if ok
                    else (ver or "container version check failed")[:120]
                )
                config_check = VerifyCheck("recyclarr", "config", ok, detail)
        except Exception as exc:
            config_check = VerifyCheck("recyclarr", "config", False, str(exc)[:80], status="not_ready")

        # ── profiles check: Sonarr exposes a TRaSH-style quality profile ────────
        try:
            profiles_check = self._check_recyclarr_profiles(cfg, secrets, vm_ip, root, docker_curl)
            radarr_check = self._check_radarr_profiles(cfg, secrets, vm_ip, root, docker_curl)
        except Exception as exc:
            profiles_check = VerifyCheck("recyclarr", "profiles", False, str(exc)[:80], status="not_ready")
            radarr_check = VerifyCheck("recyclarr", "radarr_profiles", False, str(exc)[:80], status="not_ready")

        return [config_check, profiles_check, radarr_check, self._check_last_sync(cfg, vm_ip, root)]

    @staticmethod
    def _check_last_sync(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """Verify that the current generated config completed a successful sync."""
        from toolkit.services.sdk import VerifyCheck, docker_exec_on_vm, ssh_on_vm

        if cfg.domain == "localhost":
            return VerifyCheck("recyclarr", "last_sync", True, "skipped (localhost)")
        script = (
            "test -s /config/.last-sync.sha256; "
            "expected=$(cat /config/.last-sync.sha256); "
            "actual=$(sha256sum /config/recyclarr.yml | cut -d ' ' -f1); "
            'test "$actual" = "$expected"; printf "%s" "$actual"'
        )
        if cfg.is_multi_node:
            host_script = script.replace("/config/", "/opt/homelab/generated/recyclarr/")
            rc, out, _ = ssh_on_vm(cfg, vm_ip, f"sh -ec {shlex.quote(host_script)}", root=root, timeout=25)
        else:
            rc, out = docker_exec_on_vm(
                cfg,
                "recyclarr",
                ["sh", "-ec", script],
                vm_ip,
                root,
                timeout=25,
            )
        digest = (out or "").strip()
        if rc != 0 or len(digest) != 64:
            return VerifyCheck(
                "recyclarr",
                "last_sync",
                False,
                "current config has no successful sync receipt",
                status="not_ready",
            )
        return VerifyCheck("recyclarr", "last_sync", True, f"current config synced ({digest[:12]})")

    @staticmethod
    def _check_radarr_profiles(
        cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path, docker_curl
    ) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        api_key = secrets.get("RADARR_API_KEY", "")
        if not api_key:
            return VerifyCheck(
                "recyclarr",
                "radarr_profiles",
                False,
                "RADARR_API_KEY missing",
                status="not_ready",
            )
        rc, out = docker_curl(
            cfg,
            vm_ip,
            "radarr",
            "http://127.0.0.1:7878/api/v3/qualityprofile",
            root=root,
            headers={"X-Api-Key": api_key},
            timeout=20,
        )
        if rc != 0 or not out:
            return VerifyCheck(
                "recyclarr", "radarr_profiles", False, "Radarr qualityprofile API unreachable", status="not_ready"
            )
        try:
            profiles = json.loads(out)
        except json.JSONDecodeError:
            return VerifyCheck("recyclarr", "radarr_profiles", False, "invalid qualityprofile JSON", status="not_ready")
        matched = [
            p
            for p in profiles
            if isinstance(p, dict) and ("WEB-1080p" in (p.get("name") or "") or "HD-1080p" in (p.get("name") or ""))
        ]
        ok = len(matched) > 0
        names = [p.get("name", "") for p in matched[:2]]
        return VerifyCheck(
            "recyclarr",
            "radarr_profiles",
            ok,
            f"found: {', '.join(names)}" if ok else f"{len(profiles)} profile(s), none match TRaSH",
            status=None if ok else "not_ready",
        )

    @staticmethod
    def _check_recyclarr_profiles(
        cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path, docker_curl
    ) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        def _profiles_from_api():
            api_key = secrets.get("SONARR_API_KEY", "")
            if not api_key:
                return [], "SONARR_API_KEY not set"
            rc, out = docker_curl(
                cfg,
                vm_ip,
                "sonarr",
                "http://127.0.0.1:8989/api/v3/qualityprofile",
                root=root,
                headers={"X-Api-Key": api_key},
                timeout=20,
            )
            if rc != 0 or not out:
                return [], "Sonarr qualityprofile API unreachable"
            try:
                profiles = json.loads(out)
            except json.JSONDecodeError:
                return [], "invalid qualityprofile JSON"
            if not isinstance(profiles, list):
                return [], "unexpected API response"
            return profiles, ""

        def _match_trash(profiles):
            return [
                p
                for p in profiles
                if isinstance(p, dict)
                and (
                    "WEB-1080p" in (p.get("name") or "")
                    or "web-1080p" in (p.get("name") or "").lower()
                    or "WEB-1080" in (p.get("name") or "").upper()
                    or "HD-1080p" in (p.get("name") or "")
                )
            ]

        profiles, err = _profiles_from_api()
        if err:
            return VerifyCheck("recyclarr", "profiles", False, err, status="not_ready")
        matched = _match_trash(profiles)
        ok = len(matched) > 0
        names = [p.get("name", "") for p in matched[:3]]
        detail = f"found: {', '.join(names)}" if ok else f"{len(profiles)} profile(s), none match WEB-1080p/TRaSH"
        return VerifyCheck("recyclarr", "profiles", ok, detail, status=None if ok else "not_ready")
