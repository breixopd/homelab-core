"""Workstation safety guards."""

from __future__ import annotations

import os

import pytest
from toolkit.core.ops.controller_guard import (
    allow_env,
    is_dedicated_deploy_controller,
    is_guest_runtime,
    skip_on_workstation,
)


@pytest.fixture(autouse=True)
def _clear_guard_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HOMELAB_"):
            monkeypatch.delenv(key, raising=False)


def test_guest_runtime_allows_host_ops(monkeypatch):
    monkeypatch.setenv("HOMELAB_NODE", "infra")
    assert is_guest_runtime()
    assert skip_on_workstation("docker_prune") is False


def test_workstation_skips_by_default(monkeypatch):
    assert skip_on_workstation("docker_prune") is True


def test_dedicated_controller_allows(monkeypatch):
    monkeypatch.setenv("HOMELAB_DEPLOY_CONTROLLER", "1")
    assert is_dedicated_deploy_controller()
    assert skip_on_workstation("docker_prune") is False


def test_per_operation_override(monkeypatch):
    monkeypatch.setenv("HOMELAB_ALLOW_DNS_SYNC", "1")
    assert allow_env("dns_sync")
    assert skip_on_workstation("dns_sync") is False
