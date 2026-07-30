"""navidrome service plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class NavidromePlugin(ServicePlugin):
    service = "navidrome"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            resolve_bootstrap_password,
        )

        checks: list[VerifyCheck] = []
        if not container_exists_on_vm(cfg, vm_ip, "navidrome", root):
            return [VerifyCheck("navidrome", "ping", False, "container missing")]

        rc, _ = docker_curl(cfg, vm_ip, "navidrome", "http://localhost:4533/ping", root=root)
        checks.append(VerifyCheck("navidrome", "ping", rc == 0, "reachable" if rc == 0 else "unreachable"))

        rc_env, out = docker_exec_on_vm(cfg, "navidrome", ["env"], vm_ip, root)
        if rc_env != 0:
            checks.append(VerifyCheck("navidrome", "external_auth", False, "could not read env"))
            return checks
        env: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        from toolkit.core.infra.edge_network import edge_network_values
        from toolkit.core.manifest.placement import service_address

        _, caddy_ip = edge_network_values(cfg)
        expected_source = f"{service_address(cfg, 'caddy')}/32" if cfg.is_multi_node else f"{caddy_ip}/32"
        source = env.get("ND_EXTAUTH_TRUSTEDSOURCES", "")
        user_header = env.get("ND_EXTAUTH_USERHEADER", "")
        editing_disabled = env.get("ND_ENABLEUSEREDITING", "").lower() == "false"
        external_auth_ok = source == expected_source and user_header == "Remote-User" and editing_disabled
        detail = (
            f"trusted proxy {source}, header {user_header}"
            if external_auth_ok
            else f"expected trusted proxy {expected_source}/Remote-User with user editing disabled"
        )
        checks.append(VerifyCheck("navidrome", "external_auth", external_auth_ok, detail))

        checks.append(self._check_library(cfg, secrets, vm_ip, root, resolve_bootstrap_password))
        return checks

    @staticmethod
    def _check_library(cfg, secrets, vm_ip, root, resolve_bootstrap_password) -> VerifyCheck:
        """Music library readable — song count via mount probe or Subsonic API."""
        from toolkit.services.sdk import VerifyCheck, docker_exec_on_vm

        find_audio = (
            "find /music -type f \\( -iname '*.mp3' -o -iname '*.flac' "
            "-o -iname '*.m4a' -o -iname '*.ogg' \\) 2>/dev/null | head -200 | wc -l"
        )
        count_cmd = ["sh", "-c", find_audio]
        rc, out = docker_exec_on_vm(cfg, "navidrome", count_cmd, vm_ip, root, timeout=30)
        count = 0
        if rc == 0 and (out or "").strip().isdigit():
            count = int(out.strip())
            if count > 0:
                return VerifyCheck("navidrome", "library", True, f"{count}+ audio file(s) on /music mount")

        admin_pass = resolve_bootstrap_password(secrets, "SSO_USER_PASSWORD")
        mount_cmd = ["sh", "-c", "test -d /music && ls -A /music 2>/dev/null | head -1"]
        mrc, mout = docker_exec_on_vm(cfg, "navidrome", mount_cmd, vm_ip, root, timeout=15)
        mount_ok = mrc == 0
        if not admin_pass:
            if mount_ok and not (mout or "").strip():
                return VerifyCheck(
                    "navidrome",
                    "library",
                    True,
                    "library empty (no music files on share)",
                )
            return VerifyCheck("navidrome", "library", False, "no audio files found on /music")
        # Navidrome's image already provides curl for its existing probes, but
        # does not guarantee a Python runtime. The password is expanded only
        # inside the container after the stdin-backed wrapper injects it.
        sub_script = (
            "curl -sfG http://127.0.0.1:4533/rest/getIndexes "
            "--data-urlencode u=admin "
            '--data-urlencode "p=$HOMELAB_VERIFY_PASSWORD" '
            "--data-urlencode v=1.16.1 "
            "--data-urlencode c=homelab-verify "
            "--data-urlencode f=json"
        )
        src, sout = docker_exec_on_vm(
            cfg,
            "navidrome",
            ["sh", "-c", sub_script],
            vm_ip,
            root,
            timeout=20,
            secret_environment={"HOMELAB_VERIFY_PASSWORD": admin_pass},
        )
        if src == 0 and sout:
            try:
                data = json.loads(sout)
                indexes = data.get("subsonic-response", {}).get("indexes", {}).get("index", [])
                if isinstance(indexes, dict):
                    indexes = [indexes]
                artists = sum(
                    len(ix.get("artist", [])) if isinstance(ix.get("artist"), list) else 1
                    for ix in indexes
                    if isinstance(ix, dict)
                )
                if artists > 0:
                    return VerifyCheck("navidrome", "library", True, f"{artists} artist(s) indexed")
            except json.JSONDecodeError:
                pass
        if mount_ok:
            return VerifyCheck(
                "navidrome",
                "library",
                True,
                "library empty (no music files on share)",
            )
        return VerifyCheck("navidrome", "library", False, "music mount empty or library not scanned")
