"""Wazuh manager agent-list helpers — cfg-aware."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "wazuh_agent_control_cmd",
    "wazuh_list_agents",
    "wazuh_parse_agent_lines",
]


def wazuh_agent_control_cmd() -> str:
    """Remote command to list registered Wazuh agents."""
    return "/var/ossec/bin/agent_control -l"


@dataclass(frozen=True)
class WazuhAgentSummary:
    """Parsed agent-list summary."""

    total: int
    active: int
    lines: list[str]


def wazuh_parse_agent_lines(output: str) -> WazuhAgentSummary:
    """Parse ``agent_control -l`` output into agent counts."""
    lines = [
        line
        for raw_line in (output or "").splitlines()
        if (line := raw_line.strip()) and not line.lower().startswith("available agents")
    ]
    active = sum(1 for ln in lines if "Active" in ln or "active" in ln.lower())
    return WazuhAgentSummary(total=len(lines), active=active, lines=lines)


def wazuh_list_agents(
    cfg: Config,
    vm_ip: str,
    root: Path,
    *,
    timeout: int = 30,
) -> tuple[WazuhAgentSummary | None, str]:
    """Run agent_control on the Wazuh manager host and return parsed summary."""
    import subprocess

    from toolkit.services.sdk._vmexec import ssh_on_vm

    cmd = wazuh_agent_control_cmd()
    if cfg.is_multi_node:
        rc, out, err = ssh_on_vm(cfg, vm_ip, cmd, root=root, timeout=timeout)
        if rc != 0:
            return None, (err or out or "agent_control failed")[:120]
    else:
        try:
            proc = subprocess.run(
                ["/var/ossec/bin/agent_control", "-l"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = proc.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)[:120]
    return wazuh_parse_agent_lines(out or ""), ""
