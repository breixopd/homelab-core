"""wazuh-dashboard service plugin — owns verify() on top of service.yaml defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services.sdk import VerifyCheck


def _systemd_active(unit: str, *, attempts: int = 12, interval: float = 5.0) -> bool:
    import subprocess
    import time

    for attempt in range(attempts):
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip() == "active":
            return True
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False


def _ntfy_integration_installed() -> bool:
    """Report whether the generated Wazuh notification integration is present."""
    return Path("/var/ossec/integrations/custom-ntfy").is_file()


class WazuhPlugin(ServicePlugin):
    service = "wazuh-dashboard"
    category = "security"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Report the host Wazuh manager and notification integration state."""
        import subprocess

        logs = [
            "Wazuh -> ntfy integration: installed"
            if _ntfy_integration_installed()
            else "Wazuh -> ntfy integration: not installed (deploy security role)"
        ]
        if not _systemd_active("wazuh-manager"):
            logs.append("WARNING: Wazuh Manager: systemd unit not active")
            return logs

        logs.append("Wazuh Manager: service active")
        try:
            proc = subprocess.run(
                ["/var/ossec/bin/wazuh-control", "status"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            required = (
                "wazuh-db is running",
                "wazuh-remoted is running",
                "wazuh-analysisd is running",
                "wazuh-apid is running",
            )
            missing = [daemon for daemon in required if daemon not in output]
            if missing:
                logs.append(f"WARNING: Wazuh Manager: missing daemons ({', '.join(missing)})")
            else:
                logs.append("Wazuh Manager: core daemons running")
        except OSError as exc:
            logs.append(f"WARNING: Wazuh Manager: status check failed ({exc})")
        return logs

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "wazuh-agent":
            raise ValueError(f"unsupported Wazuh host integration: {integration}")
        from toolkit.services.sdk import systemd_unit_active

        active = systemd_unit_active(root, host, "wazuh-agent")
        if active is True:
            return True, "Wazuh agent active"
        if active is False:
            return False, "Wazuh agent inactive"
        return None, "could not query Wazuh agent"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import (
            VerifyCheck,
            basic_auth_header,
            container_exists_on_vm,
            docker_curl,
            wazuh_list_agents,
        )

        if cfg.domain == "localhost" or not cfg.category_enabled("security"):
            return [VerifyCheck("wazuh", "skipped", True, "skipped (localhost or security disabled)")]

        checks: list[VerifyCheck] = []

        if not container_exists_on_vm(cfg, vm_ip, "wazuh-dashboard", root):
            return [VerifyCheck("wazuh", "dashboard_api", False, "container missing")]

        # ── dashboard_api — login page / API reachable inside container ───────
        rc, out = docker_curl(
            cfg,
            vm_ip,
            "wazuh-dashboard",
            "https://wazuh.dashboard:5601/app/login",
            root=root,
            ca_file="/usr/share/wazuh-dashboard/certs/root-ca.pem",
            timeout=15,
        )
        api_ok = rc == 0 and bool((out or "").strip())
        checks.append(
            VerifyCheck(
                "wazuh",
                "dashboard_api",
                api_ok,
                "reachable" if api_ok else (out or "unreachable")[:120],
            )
        )

        # ── indexer_link — dashboard can reach indexer cluster health ─────────
        password = secrets.get("WAZUH_INDEXER_PASSWORD", "")
        if not password:
            checks.append(VerifyCheck("wazuh", "indexer_link", False, "WAZUH_INDEXER_PASSWORD not set"))
        else:
            rc_idx, idx_out = docker_curl(
                cfg,
                vm_ip,
                "wazuh-dashboard",
                "https://wazuh.indexer:9200/_cluster/health",
                root=root,
                headers={"Authorization": basic_auth_header("admin", password)},
                ca_file="/usr/share/wazuh-dashboard/certs/root-ca.pem",
                timeout=15,
            )
            idx_ok = False
            idx_detail = (idx_out or "unreachable")[:120]
            if rc_idx == 0 and idx_out:
                try:
                    payload = json.loads(idx_out)
                    status = (payload.get("status") or "").lower() if isinstance(payload, dict) else ""
                except json.JSONDecodeError:
                    status = ""
                if status in ("green", "yellow"):
                    idx_ok = True
                    idx_detail = f"indexer status={status}"
            checks.append(VerifyCheck("wazuh", "indexer_link", idx_ok, idx_detail))

        # ── agents — manager agent inventory (skip when fleet not deployed) ───
        summary, err = wazuh_list_agents(cfg, vm_ip, root)
        if summary is None:
            checks.append(VerifyCheck("wazuh", "manager", False, err or "manager unavailable"))
            return checks
        checks.append(VerifyCheck("wazuh", "manager", True, "agent inventory available"))
        min_agents = max(1, len(cfg.enabled_nodes) - 1) if cfg.is_multi_node else 1
        if summary.total == 0:
            checks.append(VerifyCheck("wazuh", "agents", False, f"0/0 agents active (need ≥{min_agents})"))
            return checks
        checks.append(
            VerifyCheck(
                "wazuh",
                "agents",
                summary.active >= min_agents,
                f"{summary.active}/{summary.total} agents active (need ≥{min_agents})",
            )
        )
        return checks
