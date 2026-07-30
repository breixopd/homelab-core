from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.config.config import ProjectEntry


@pytest.mark.parametrize("value", ["x;id", "../x", "x$(id)", "x x"])
def test_project_rejects_shell_metacharacters(value: str):
    with pytest.raises(ValidationError):
        ProjectEntry(
            subdomain=value,
            auth_mode="forward_auth",
            exposure="private",
            docker_image="nginx:1@sha256:" + "a" * 64,
            container_port=8080,
            placement="apps",
        )


def test_project_route_policy_is_explicit_and_strict() -> None:
    base = {
        "subdomain": "demo",
        "auth_mode": "forward_auth",
        "exposure": "private",
        "docker_image": "docker.io/library/nginx:1@sha256:" + "a" * 64,
        "container_port": 8080,
        "placement": "apps",
    }
    entry = ProjectEntry.model_validate(base)

    assert entry.auth_mode == "forward_auth"
    assert entry.exposure == "private"
    assert entry.upstream == "demo:8080"
    for field in ("auth_mode", "exposure", "placement"):
        invalid = dict(base)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            ProjectEntry.model_validate(invalid)
    with pytest.raises(ValidationError):
        ProjectEntry.model_validate({**base, "auth_mode": "oidc"})
    with pytest.raises(ValidationError):
        ProjectEntry.model_validate({**base, "exposure": "internal"})
    with pytest.raises(ValidationError):
        ProjectEntry.model_validate({**base, "auth": True})
    with pytest.raises(ValidationError):
        ProjectEntry.model_validate({**base, "node": "apps"})
