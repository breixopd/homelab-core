"""Cfg-aware, multi-VM execution helpers for service plugins.

Internal implementation module — plugins import these via ``toolkit.services.sdk``.
Branches on ``cfg.is_multi_node`` and delegates to ``toolkit.core.ansible.ansible_ssh``
for Proxmox jump-host reaches.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.net.curl_config import DEFAULT_PROBE_RESPONSE_BYTES

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "ssh_on_vm",
    "docker_curl",
    "docker_exec_on_vm",
    "docker_health_status_on_vm",
    "container_exists_on_vm",
    "parse_curl_headers",
]


def docker_exec_on_vm(
    cfg: Config,
    container: str,
    cmd: list[str],
    vm_ip: str,
    root: Path,
    *,
    timeout: int = 30,
    user: str = "",
    env: dict[str, str] | None = None,
    secret_environment: dict[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[int, str]:
    """Run ``cmd`` inside ``container``; local subprocess or SSH for multi-VM.

    ``secret_environment`` is delivered to a static in-container wrapper over
    stdin, preventing it from appearing in local or remote process arguments.
    """
    from toolkit.core.ops.secret_env import wrap_command_with_secret_environment

    wrapped_cmd, input_payload = wrap_command_with_secret_environment(
        cmd,
        environment=env,
        secret_environment=secret_environment,
        stdin=stdin,
    )
    user_flag = f"-u {shlex.quote(user)} " if user else ""
    env_flags = "".join(f"-e {shlex.quote(f'{k}={v}')} " for k, v in (env or {}).items())
    env_args = [part for k, v in (env or {}).items() for part in ("-e", f"{k}={v}")]
    if cfg.is_multi_node:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        cmd_str = " ".join(shlex.quote(c) for c in wrapped_cmd)
        interactive = "-i " if input_payload is not None else ""
        rc, out, _ = ssh_run_on_vm(
            cfg,
            vm_ip,
            f"docker exec {interactive}{user_flag}{env_flags}{shlex.quote(container)} {cmd_str}",
            root=root,
            timeout=timeout,
            stdin=input_payload,
        )
        return rc, out or ""
    user_args = ["-u", user] if user else []
    interactive_args = ["-i"] if input_payload is not None else []
    try:
        proc = subprocess.run(
            ["docker", "exec", *interactive_args, *user_args, *env_args, container, *wrapped_cmd],
            capture_output=True,
            input=input_payload,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def ssh_on_vm(
    cfg: Config,
    vm_ip: str,
    command: str,
    *,
    root: Path | None = None,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a shell command on the target VM (Proxmox-aware; local when loopback)."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    return ssh_run_on_vm(cfg, vm_ip, command, root=root, timeout=timeout)


def docker_curl(
    cfg: Config,
    vm_ip: str,
    container: str,
    url: str,
    *,
    root: Path | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: str | None = None,
    ca_file: str | None = None,
    cookie_file: str | None = None,
    cookie_jar: str | None = None,
    timeout: int = 15,
    max_response_bytes: int | None = DEFAULT_PROBE_RESPONSE_BYTES,
) -> tuple[int, str]:
    """curl ``url`` from inside a container on the target VM via SSH."""
    from toolkit.core.ansible.ansible_ssh import docker_exec_curl

    return docker_exec_curl(
        cfg,
        vm_ip,
        container,
        url,
        root=root,
        headers=headers,
        method=method,
        body=body,
        ca_file=ca_file,
        cookie_file=cookie_file,
        cookie_jar=cookie_jar,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
    )


def docker_health_status_on_vm(cfg: Config, vm_ip: str, container: str, root: Path) -> tuple[str, str]:
    """Container ``docker inspect`` state + health on the target VM."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    fmt_state = "{{.State.Status}}"
    fmt_health = "{{.State.Health.Status}}"
    _state, _health = "", ""
    rc, out, _ = ssh_run_on_vm(
        cfg,
        vm_ip,
        f"docker inspect --format {shlex.quote(fmt_state)} {shlex.quote(container)} 2>/dev/null",
        root=root,
        timeout=15,
    )
    if rc == 0:
        _state = (out or "").strip().lower()
    rc, out, _ = ssh_run_on_vm(
        cfg,
        vm_ip,
        f"docker inspect --format {shlex.quote(fmt_health)} {shlex.quote(container)} 2>/dev/null",
        root=root,
        timeout=15,
    )
    if rc == 0:
        _health = (out or "").strip().lower()
    if _health in ("<no value>", "<nil>", "none"):
        _health = ""
    return _state, _health


def container_exists_on_vm(cfg: Config, vm_ip: str, container: str, root: Path) -> bool:
    """True when a named container is present on the target VM."""
    if cfg.is_multi_node:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        rc, _, _ = ssh_run_on_vm(
            cfg,
            vm_ip,
            f"docker inspect --format '{{{{.Name}}}}' {shlex.quote(container)} >/dev/null 2>&1",
            root=root,
            timeout=15,
        )
        return rc == 0
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", container],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def parse_curl_headers(output: str) -> tuple[int | None, dict[str, str]]:
    """Parse ``curl -I`` / ``curl -skI`` output into ``(status, headers)``."""
    status: int | None = None
    headers: dict[str, str] = {}
    for line in output.splitlines():
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        elif ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    return status, headers
