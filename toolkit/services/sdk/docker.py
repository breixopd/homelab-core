"""Local Docker primitives — cfg-free leaf module.

For managed-machine execution, use the cfg-aware helpers in
``toolkit.services.sdk._vmexec``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

__all__ = [
    "docker_exec",
    "docker_health_status",
    "container_exists",
    "resolve_service_url",
]


def _local_network_ips() -> list[str]:
    """IPv4 addresses bound to local interfaces (excludes loopback)."""
    try:
        out = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [tok for tok in out.split() if not tok.startswith("127.") and ":" not in tok]


def _is_local_ip(ip: str) -> bool:
    """True for loopback/localhost or an IP bound to a local interface."""
    if ip in ("127.0.0.1", "::1", "localhost", ""):
        return True
    return ip in _local_network_ips()


def docker_exec(
    service: str,
    command: list[str],
    *,
    vm_ip: str = "localhost",
    root: str | Path | None = None,
    timeout: int = 120,
    user: str | None = None,
) -> tuple[int, str]:
    """Run ``command`` inside container ``service`` via ``docker exec``."""
    if _is_local_ip(vm_ip):
        try:
            args = ["docker", "exec"]
            if user:
                args += ["-u", user]
            args += [service, *command]
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)

    raise ValueError("remote Docker execution requires docker_exec_on_vm with a Config")


def docker_health_status(
    container: str,
    *,
    vm_ip: str = "localhost",
    root: str | Path | None = None,
) -> tuple[str, str]:
    """Inspect ``container`` and return ``(state, health)``."""
    fmt = "{{.State.Status}}\n{{.State.Health.Status}}"
    if _is_local_ip(vm_ip):
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format", fmt, container],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode != 0:
                return "", ""
            out = (proc.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            return "", ""
    else:
        raise ValueError("remote Docker inspection requires docker_health_status_on_vm with a Config")

    if not out:
        return "", ""
    parts = out.splitlines()
    state = parts[0].strip().lower() if parts else ""
    health = parts[1].strip().lower() if len(parts) > 1 else ""
    if health in ("<no value>", "<nil>", "none"):
        health = ""
    return state, health


def container_exists(
    name: str,
    *,
    vm_ip: str = "localhost",
    root: str | Path | None = None,
) -> bool:
    """True when a Docker container named ``name`` exists on ``vm_ip``."""
    if _is_local_ip(vm_ip):
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}}", name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    raise ValueError("remote Docker inspection requires container_exists_on_vm with a Config")


def resolve_service_url(
    service: str,
    port: int,
    fallback_host: str = "localhost",
) -> str:
    """Return a reachable ``http://<ip>:<port>`` URL for a compose service."""

    def _container_ip(name: str) -> str:
        try:
            out = subprocess.run(
                ["docker", "inspect", name, "-f", "{{json .NetworkSettings.Networks}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode != 0:
                return ""
            networks = json.loads((out.stdout or "").strip() or "{}")
            for net in networks.values():
                ip = (net or {}).get("IPAddress", "")
                if ip:
                    return ip
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, TypeError):
            pass
        return ""

    ip = _container_ip(service)
    if ip:
        return f"http://{ip}:{port}"
    return f"http://{fallback_host}:{port}"
