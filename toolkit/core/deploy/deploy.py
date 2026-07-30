from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from toolkit.core.compose.docker import DockerCompose, compose_for_root, profiles_for_categories
from toolkit.core.compose.registry import dependency_sort, enabled_categories, load_all
from toolkit.core.config.config import Config
from toolkit.core.config.storage import env_path


@dataclass
class DeployResult:
    vm: str
    success: bool
    services_started: list[str] = field(default_factory=list)
    services_failed: list[str] = field(default_factory=list)
    error: str = ""


def health_gate(
    compose: DockerCompose,
    services: list[str],
    timeout: int = 120,
    poll_interval: int = 5,
) -> dict[str, bool]:
    """Wait for services to become healthy. Returns dict of service->healthy."""
    deadline = time.time() + timeout
    results = {svc: False for svc in services}

    while time.time() < deadline:
        containers = compose.ps()
        for c in containers:
            if c.service in results:
                # Services must have a passing healthcheck to be considered healthy.
                # A service without a healthcheck (health="") is treated as unhealthy
                # since all production services should have explicit healthchecks.
                running = c.state == "running"
                healthy = c.health == "healthy"
                if running and healthy:
                    results[c.service] = True
        if all(results.values()):
            break
        time.sleep(poll_interval)

    return results


def deploy_local(root: Path, vm: str, config: Config) -> DeployResult:
    """Deploy services for a VM to the local Docker socket.

    Remote LXCs are handled by deploy-server-toolkit.yml (Ansible).
    """
    load_all()
    cats = enabled_categories(config)
    vm_cats = dependency_sort([c for c in cats if c.runtime_node(config) == vm])

    result = DeployResult(vm=vm, success=True)

    env_file = env_path(vm, root)
    if not env_file.exists():
        return DeployResult(vm=vm, success=False, error=f"No .env file for {vm}")

    if not vm_cats:
        return result

    profiles = profiles_for_categories(vm_cats, config)

    compose = compose_for_root(config, root, vm=vm)
    if compose is None:
        return DeployResult(vm=vm, success=False, error=f"Missing Compose model for {vm}")
    if not compose.pull_retry(profiles=profiles):
        return DeployResult(vm=vm, success=False, error="Failed docker compose pull (after retries)")
    if not compose.up(profiles=profiles):
        return DeployResult(vm=vm, success=False, error="Failed docker compose up")

    svc_names = [s.name for cat in vm_cats for s in cat.services(config)]
    health = health_gate(compose, svc_names, timeout=120)
    for svc, healthy in health.items():
        if healthy:
            result.services_started.append(svc)
        else:
            result.services_failed.append(svc)

    if result.services_failed:
        result.success = False
        result.error = f"Unhealthy: {', '.join(result.services_failed)}"

    return result


def deploy_all(root: Path, config: Config) -> dict[str, bool]:
    """Deploy to all enabled VMs. Returns {vm: success}.

    All VMs are deployed to the local Docker socket (for the infra LXC).
    Remote LXCs are handled by deploy-server-toolkit.yml (Ansible).
    """
    results = {}
    for vm in config.enabled_nodes:
        results[vm] = deploy_local(root, vm, config).success
    return results
