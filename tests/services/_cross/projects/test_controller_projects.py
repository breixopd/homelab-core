from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    create_project,
    read_projects_view,
    remove_project,
)
from toolkit.controller.read_models import ProjectCreate, ProjectDefinition, ProjectRemove
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def test_project_crud_is_revision_guarded(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    initial = read_projects_view(tmp_path)
    project = ProjectDefinition(
        name="Status",
        subdomain="status",
        auth_mode="forward_auth",
        exposure="private",
        description="Internal status",
        show_on_portal=True,
        docker_image="docker.io/library/nginx:1@sha256:" + "a" * 64,
        container_port=45_678,
        placement="apps",
        database_service="dev-postgres",
    )

    created = create_project(
        tmp_path,
        ProjectCreate(expected_revision=initial.revision, project=project),
    )

    assert [entry.subdomain for entry in created.projects] == ["status"]
    assert created.projects[0].auth_mode == "forward_auth"
    assert created.projects[0].exposure == "private"
    assert created.projects[0].placement == "apps"
    assert created.projects[0].node == "apps"
    assert created.projects[0].database_service == "dev-postgres"
    assert any(option.selector == "apps" and option.kind == "capability" for option in created.available_placements)
    assert any(
        option.service == "dev-postgres" and option.engine == "postgresql" for option in created.available_databases
    )
    with pytest.raises(DesiredStateConflictError):
        remove_project(
            tmp_path,
            ProjectRemove(expected_revision=initial.revision, subdomain="status"),
        )
    removed = remove_project(
        tmp_path,
        ProjectRemove(expected_revision=created.revision, subdomain="status"),
    )
    assert removed.projects == []
