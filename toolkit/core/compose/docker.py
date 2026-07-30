from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


def compose_process_environment(
    env_file: Path | None,
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Keep ambient variables from overriding an explicit Compose env file."""
    env = dict(os.environ)
    if env_file and env_file.is_file():
        from dotenv import dotenv_values

        for name in dotenv_values(env_file):
            env.pop(name, None)
    if overrides:
        env.update(overrides)
    return env


def deployment_compose_path(cfg, root: Path, role: str) -> Path:
    """Return the only Compose model that runtime operations may use on this node."""
    if cfg.is_multi_node:
        return root / "generated" / role / "compose.yaml"
    return root / "docker-compose.yml"


def vm_role_for_compose(cfg, vm_hint: str | None = None) -> str:
    """Which node's generated environment to use for Compose on this machine.

    Single-node configs use the configured control node. Multi-node configs use
    an explicit hint or ``HOMELAB_NODE``, falling back to the control node.
    """
    if not cfg.is_multi_node:
        return cfg.control_node
    if vm_hint:
        return vm_hint
    return os.environ.get("HOMELAB_NODE", cfg.control_node)


def compose_for_root(cfg, root: Path, *, vm: str | None = None) -> DockerCompose | None:
    """Select the full single-node model or the least-privilege model for one node role."""
    from toolkit.core.config.storage import env_path

    role = vm_role_for_compose(cfg, vm)
    main = deployment_compose_path(cfg, root, role)
    if not main.exists():
        return None
    return DockerCompose(compose_file=main, env_file=env_path(role, root))


def profiles_for_categories(cats: list, cfg=None) -> list[str]:
    """Flatten compose_profiles for categories (dedupe, stable sort)."""
    parts: list[str] = []
    for cat in cats:
        if cfg is not None and hasattr(cat, "selected_compose_profiles"):
            parts.extend(cat.selected_compose_profiles(cfg))
        else:
            parts.extend(cat.compose_profiles or [cat.name])
    return sorted(set(parts))


def compose_for_category(cat, cfg, root: Path) -> DockerCompose:
    """DockerCompose for operations scoped to one category's node role."""
    role = cat.runtime_node(cfg)
    dc = compose_for_root(cfg, root, vm=role if cfg.is_multi_node else None)
    if dc is None:
        expected = deployment_compose_path(cfg, root, role)
        raise FileNotFoundError(f"Compose model missing for {role}: {expected}")
    return dc


@dataclass
class ContainerStatus:
    name: str
    service: str
    state: str
    health: str
    image: str


class DockerCompose:
    """Wrapper for docker compose CLI operations."""

    def __init__(self, compose_file: Path, env_file: Path | None = None, project_name: str | None = None):
        self.compose_file = compose_file
        self.env_file = env_file
        self.project_name = project_name

    def _base_cmd(self) -> list[str]:
        cmd = ["docker", "compose", "-f", str(self.compose_file)]
        if self.env_file:
            cmd.extend(["--env-file", str(self.env_file)])
        if self.project_name:
            cmd.extend(["-p", self.project_name])
        return cmd

    def _run(
        self,
        args: list[str],
        check: bool = False,
        capture: bool = True,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess:
        """Run docker compose command.

        All public methods use check=False (the default) so callers can inspect
        result.returncode without raising CalledProcessError. This is the intended
        pattern: call the method, then explicitly check returncode == 0 on the result.
        """
        cmd = self._base_cmd() + args
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            timeout=timeout,
            env=compose_process_environment(self.env_file),
        )

    def preflight(self, timeout: int = 15) -> bool:
        """Check Docker daemon is reachable.

        Runs `docker info` with the given timeout. Returns True if the daemon
        responds (returncode 0), False otherwise. Use this before deploy operations
        to fail fast if Docker is unavailable.
        """
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False
        except subprocess.TimeoutExpired:
            return False

    def up(
        self,
        services: list[str] | None = None,
        detach: bool = True,
        profiles: list[str] | None = None,
        *,
        force_recreate: bool = False,
    ) -> bool:
        args = ["up"]
        if detach:
            args.append("-d")
        if force_recreate:
            args.append("--force-recreate")
        if profiles:
            for p in profiles:
                args.extend(["--profile", p])
        if services:
            args.extend(services)
        result = self._run(args)
        return result.returncode == 0

    def down(self, remove_volumes: bool = False) -> bool:
        args = ["down"]
        if remove_volumes:
            args.append("-v")
        result = self._run(args)
        return result.returncode == 0

    def pull(self, services: list[str] | None = None, profiles: list[str] | None = None) -> bool:
        args = ["pull"]
        if profiles:
            for p in profiles:
                args.extend(["--profile", p])
        if services:
            args.extend(services)
        result = self._run(args)
        return result.returncode == 0

    # Transient failure markers that justify a docker compose pull retry
    # (rate-limited registry response, a flaky network blip, a temporary 503).
    _PULL_RETRY_MARKERS = (
        "toomanyrequests",
        "manifest unknown",
        "i/o timeout",
        "connection refused",
        "tls: failed to verify",
        "503 service",
        "service unavailable",
    )

    def pull_retry(
        self,
        services: list[str] | None = None,
        profiles: list[str] | None = None,
        *,
        retries: int = 3,
        backoff_s: int = 5,
    ) -> bool:
        """docker compose pull with bounded retry on transient failures.

        The deploy path uses this so a flaky pull (rate-limited registry,
        network blip) doesn't abort the whole deploy. Non-retryable failures
        (bad image name, auth 401) fail fast on the first attempt.
        """
        args = ["pull"]
        if profiles:
            for p in profiles:
                args.extend(["--profile", p])
        if services:
            args.extend(services)
        for attempt in range(retries):
            result = self._run(args)
            if result.returncode == 0:
                return True
            stderr = (result.stderr or "").lower()
            if attempt + 1 < retries and any(m in stderr for m in self._PULL_RETRY_MARKERS):
                time.sleep(backoff_s)
                continue
            return False
        return False

    def ps(self, *, timeout: int = 120) -> list[ContainerStatus]:
        try:
            result = self._run(["ps", "--format", "json", "-a"], timeout=timeout)
            containers = []
            for line in result.stdout.strip().splitlines():
                if not line:
                    continue
                data = json.loads(line)
                containers.append(
                    ContainerStatus(
                        name=data.get("Name", ""),
                        service=data.get("Service", ""),
                        state=data.get("State", "unknown"),
                        health=data.get("Health", ""),
                        image=data.get("Image", ""),
                    )
                )
            return containers
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    def logs(self, services: list[str] | None = None, tail: int = 100, follow: bool = False) -> str:
        args = ["logs", "--tail", str(tail), "--no-color"]
        if follow:
            args.append("-f")
        if services:
            args.extend(services)
        try:
            result = self._run(args, check=False)
            return result.stdout + result.stderr
        except subprocess.CalledProcessError:
            return ""

    def restart(self, services: list[str] | None = None) -> bool:
        args = ["restart"]
        if services:
            args.extend(services)
        result = self._run(args)
        return result.returncode == 0

    def image_digests(self) -> dict[str, str]:
        """Get current image ID for each running service."""
        digests = {}
        for container in self.ps():
            if container.service and container.image:
                digests[container.service] = container.image
        return digests
