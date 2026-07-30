"""Prometheus / Loki URL helpers — cfg-aware."""

from __future__ import annotations

__all__ = [
    "prometheus_url",
    "prometheus_internal_url",
    "prometheus_reload_url",
    "loki_url",
    "loki_internal_url",
]

PROMETHEUS_PORT = 9090
LOKI_PORT = 3100


def prometheus_url() -> str:
    """Docker-network Prometheus base URL (Grafana datasource default)."""
    return f"http://prometheus:{PROMETHEUS_PORT}"


def prometheus_internal_url() -> str:
    """In-container Prometheus base URL (localhost probes)."""
    return f"http://localhost:{PROMETHEUS_PORT}"


def prometheus_reload_url(*, internal: bool = False) -> str:
    """Prometheus lifecycle reload endpoint."""
    base = prometheus_internal_url() if internal else prometheus_url()
    return f"{base}/-/reload"


def loki_url() -> str:
    """Docker-network Loki base URL (Grafana datasource default)."""
    return f"http://loki:{LOKI_PORT}"


def loki_internal_url() -> str:
    """In-container Loki base URL (localhost probes)."""
    return f"http://localhost:{LOKI_PORT}"
