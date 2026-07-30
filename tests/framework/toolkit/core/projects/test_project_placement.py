from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.helpers.machines import renamed_default_machines, single_control_machines
from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig
from toolkit.core.projects.placement import default_project_placement, project_node, project_placement_options

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _project(placement: str) -> ProjectEntry:
    return ProjectEntry(
        subdomain="demo",
        auth_mode="forward_auth",
        exposure="private",
        docker_image=PINNED_IMAGE,
        placement=placement,
    )


def test_project_capability_placement_survives_machine_renames() -> None:
    cfg = Config(machines=renamed_default_machines())

    assert project_node(cfg, _project("apps")) == "data"
    assert ("apps", "data", "capability") in project_placement_options(cfg)


def test_project_may_pin_an_exact_machine() -> None:
    cfg = Config(machines=renamed_default_machines())

    assert project_node(cfg, _project("stream")) == "stream"
    assert ("stream", "stream", "machine") in project_placement_options(cfg)


def test_unspecified_project_placement_uses_the_configured_control_machine() -> None:
    cfg = Config(machines=renamed_default_machines())

    assert default_project_placement(cfg) == cfg.control_node


def test_project_placement_rejects_ambiguous_capability() -> None:
    machines = renamed_default_machines()
    machines["worker"] = machines["data"].model_copy(
        update={"hostname": "worker-01", "vmid": 899, "address": "10.10.10.99"}
    )

    with pytest.raises(ValidationError, match="matches multiple enabled machines"):
        Config(machines=machines, projects=ProjectsConfig(entries=[_project("apps")]))


def test_project_placement_rejects_unknown_selector() -> None:
    with pytest.raises(ValidationError, match="matches no enabled machine"):
        Config(projects=ProjectsConfig(entries=[_project("missing")]))


def test_single_machine_project_collapses_to_control() -> None:
    cfg = Config(machines=single_control_machines())

    assert project_node(cfg, _project("apps")) == cfg.control_node
