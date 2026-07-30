"""Docker Hub registry-mirror URL helpers — cfg-free constants + probes."""

from __future__ import annotations

__all__ = [
    "registry_mirror_port",
    "registry_mirror_ca_url",
    "registry_mirror_running",
]

REGISTRY_MIRROR_PORT = 3128


def registry_mirror_port() -> int:
    """Published registry-mirror HTTP port (3128)."""
    return REGISTRY_MIRROR_PORT


def registry_mirror_ca_url(host: str = "127.0.0.1") -> str:
    """CA certificate download URL for the registry mirror."""
    return f"http://{host}:{REGISTRY_MIRROR_PORT}/ca.crt"


def registry_mirror_running(*, docker_bin: str = "docker") -> bool:
    """True when the registry-mirror container is running."""
    import importlib

    bootstrap = importlib.import_module("toolkit.services.registry-mirror.bootstrap")
    return bootstrap._mirror_running(docker_bin=docker_bin)
