"""Redis protocol constants."""

from __future__ import annotations

__all__ = ["redis_port"]

REDIS_PORT = 6379


def redis_port() -> int:
    """Default Redis port (6379)."""
    return REDIS_PORT
