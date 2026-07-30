"""Bounded runtime actions for controller-managed project containers."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolkit.core.config.config import Config, ProjectEntry
from toolkit.core.projects.placement import project_node

ProjectCommand = Literal["start", "stop", "restart", "status", "logs"]


@dataclass(frozen=True, slots=True)
class ProjectCommandResult:
    ok: bool
    output: str
    node: str = ""


def find_project(cfg: Config, subdomain: str) -> ProjectEntry | None:
    return next((project for project in cfg.projects.entries if project.subdomain == subdomain), None)


def _command(project: ProjectEntry, action: ProjectCommand) -> list[str]:
    if action in {"start", "stop", "restart"}:
        return ["docker", action, project.subdomain]
    if action == "logs":
        return ["docker", "logs", "--tail", "200", project.subdomain]
    return ["docker", "inspect", "--format", "{{json .State}}", project.subdomain]


def run_project_command(
    root: Path,
    cfg: Config,
    subdomain: str,
    action: ProjectCommand,
) -> ProjectCommandResult:
    project = find_project(cfg, subdomain)
    if project is None:
        return ProjectCommandResult(False, f"Project '{subdomain}' is not registered")
    command = _command(project, action)
    node = project_node(cfg, project)
    if cfg.is_multi_node:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        code, stdout, stderr = ssh_run_on_vm(
            cfg,
            cfg.node_ip(node),
            shlex.join(command),
            root=root,
            timeout=60,
            retries=1,
        )
    else:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    output = (stdout if code == 0 else stderr or stdout).strip()
    if not output:
        output = f"{action} {'completed' if code == 0 else 'failed'}"
    return ProjectCommandResult(code == 0, output, node)
