"""Registry-mirror-owned startup and cache recovery operations."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, env_path

_MIRROR_VOLUME = "homelab_docker_mirror_cache"
_MIRROR_PORT = 3128


def _run(cmd: list[str], *, timeout: int = 120, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)


def _mirror_running(*, docker_bin: str = "docker") -> bool:
    proc = _run(
        [docker_bin, "inspect", "--format", "{{.State.Running}}", "registry-mirror"],
        timeout=15,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _mirror_http_ok(*, docker_bin: str = "docker") -> bool:
    if not _mirror_running(docker_bin=docker_bin):
        return False
    proc = _run(
        [
            docker_bin,
            "exec",
            "registry-mirror",
            "wget",
            "-q",
            "-O",
            "/dev/null",
            f"http://127.0.0.1:{_MIRROR_PORT}/ca.crt",
        ],
        timeout=30,
    )
    return proc.returncode == 0


def purge_registry_mirror_cache(*, docker_bin: str = "docker") -> list[str]:
    """Remove cached manifests/layers so guests stop pulling corrupt blobs."""
    logs: list[str] = []
    if _mirror_running(docker_bin=docker_bin):
        _run([docker_bin, "stop", "registry-mirror"], timeout=60)
        logs.append("Registry mirror: stopped for cache purge")

    proc = _run(
        [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{_MIRROR_VOLUME}:/cache",
            "alpine:3.21",
            "sh",
            "-c",
            "rm -rf /cache/* /cache/.[!.]* 2>/dev/null; true",
        ],
        timeout=120,
    )
    if proc.returncode == 0:
        logs.append("Registry mirror: purged docker_mirror_cache volume")
    else:
        error = (proc.stderr or proc.stdout or "unknown error")[:160]
        logs.append(f"Registry mirror: cache purge failed ({error})")
    return logs


def ensure_registry_mirror(
    root: Path | None = None,
    *,
    docker_bin: str = "docker",
    purge_cache: bool = False,
) -> list[str]:
    """Start the registry mirror on the node selected by its manifest."""
    root = Path(root or DEFAULT_HOMELAB_ROOT)
    logs: list[str] = []

    if purge_cache:
        logs.extend(purge_registry_mirror_cache(docker_bin=docker_bin))

    if _mirror_running(docker_bin=docker_bin) and _mirror_http_ok(docker_bin=docker_bin):
        return logs + ["Registry mirror: already running"]

    from toolkit.core.compose.docker import deployment_compose_path
    from toolkit.core.config.config import load_config
    from toolkit.core.manifest.placement import service_node

    cfg = load_config(root / "config.yaml")
    node = service_node(cfg, "registry-mirror")
    env_file = env_path(node, root)
    if not env_file.is_file():
        logs.append(f"Registry mirror: generated/{node}/.env missing; run homelab-toolkit generate")
        return logs
    compose_file = deployment_compose_path(cfg, root, node)
    limits_file = root / "generated" / node / "compose.limits.yml"

    up = _run(
        [
            docker_bin,
            "compose",
            "-f",
            str(compose_file),
            "--env-file",
            str(env_file),
            *(["-f", str(limits_file)] if limits_file.is_file() else []),
            "--profile",
            "svc-registry-mirror",
            "up",
            "-d",
            "registry-mirror",
        ],
        timeout=300,
        cwd=str(root),
    )
    if up.returncode != 0:
        logs.append(f"Registry mirror: start failed ({(up.stderr or up.stdout or '')[:160]})")
        return logs

    for _ in range(30):
        if _mirror_http_ok(docker_bin=docker_bin):
            logs.append("Registry mirror: running on port 3128")
            return logs
        time.sleep(2)

    logs.append("Registry mirror: started but HTTP probe did not succeed in time")
    return logs
