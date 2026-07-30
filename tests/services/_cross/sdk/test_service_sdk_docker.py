from __future__ import annotations

import pytest
from toolkit.services.sdk.docker import container_exists, docker_exec, docker_health_status


@pytest.mark.parametrize(
    "operation",
    [
        lambda: docker_exec("service", ["true"], vm_ip="192.0.2.10"),
        lambda: docker_health_status("service", vm_ip="192.0.2.10"),
        lambda: container_exists("service", vm_ip="192.0.2.10"),
    ],
)
def test_cfg_free_docker_helpers_reject_implicit_remote_ssh(operation) -> None:
    with pytest.raises(ValueError, match="remote Docker"):
        operation()
