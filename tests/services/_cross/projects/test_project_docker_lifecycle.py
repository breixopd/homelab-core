"""Contract tests for declarative custom projects."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from toolkit.core.compose.port_conflict import check_container_name, check_port_conflict
from toolkit.core.config.config import Config, ProjectEntry, load_config, save_config
from toolkit.core.config.storage import config_path

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _project(
    subdomain: str = "demo",
    *,
    port: int = 45_678,
    target: str = "apps",
) -> ProjectEntry:
    return ProjectEntry(
        name=subdomain.title(),
        subdomain=subdomain,
        auth_mode="forward_auth",
        exposure="private",
        docker_image=PINNED_IMAGE,
        container_port=port,
        placement=target,
    )


def test_project_persists_only_declarative_inputs(tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    cfg.projects.entries.append(_project())

    save_config(cfg, config_path(tmp_path))
    persisted = load_config(config_path(tmp_path)).projects.entries[0]

    assert persisted.docker_image == PINNED_IMAGE
    assert persisted.upstream == "demo:45678"
    assert "upstream:" not in config_path(tmp_path).read_text(encoding="utf-8")


def test_project_defaults_are_secure() -> None:
    entry = _project()

    assert entry.health_endpoint == ""
    assert entry.database_service == ""
    assert entry.show_on_portal is True


@pytest.mark.parametrize("image", ["", "nginx:latest", "nginx@sha256:abc"])
def test_project_requires_immutable_image(image: str) -> None:
    with pytest.raises(ValidationError, match="immutable tag and sha256 digest"):
        ProjectEntry(
            subdomain="demo",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=image,
            placement="apps",
        )


def test_project_health_endpoint_is_optional_but_safe() -> None:
    assert _project().model_copy(update={"health_endpoint": "/ready"}).health_endpoint == "/ready"
    with pytest.raises(ValidationError, match="absolute path"):
        ProjectEntry(
            subdomain="demo",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=PINNED_IMAGE,
            placement="apps",
            health_endpoint="https://example.test/ready",
        )


def test_port_conflict_is_scoped_to_target_node() -> None:
    cfg = Config()
    cfg.projects.entries = [_project("one", port=45_678, target="apps")]

    assert "One" in check_port_conflict("apps", 45_678, cfg)
    assert "One" not in check_port_conflict("media", 45_678, cfg)


def test_all_projects_using_a_conflicting_port_are_reported() -> None:
    cfg = Config()
    cfg.projects.entries = [
        _project("one", port=45_678, target="apps"),
        _project("two", port=45_678, target="apps"),
    ]

    assert check_port_conflict("apps", 45_678, cfg)[-2:] == ["One", "Two"]


@pytest.mark.parametrize("name", ["caddy", "CADDY", "postgres", "nextcloud"])
def test_managed_container_names_are_reserved(name: str) -> None:
    assert check_container_name(name, Config()) == name


def test_existing_project_name_is_reserved() -> None:
    cfg = Config()
    cfg.projects.entries = [_project("demo")]

    assert check_container_name("demo", cfg) == "demo"


def test_project_removal_preserves_other_entries() -> None:
    cfg = Config()
    cfg.projects.entries = [_project("keep"), _project("remove", port=45_679)]

    cfg.projects.entries = [entry for entry in cfg.projects.entries if entry.subdomain != "remove"]

    assert [entry.subdomain for entry in cfg.projects.entries] == ["keep"]


def test_project_database_provider_must_be_enabled() -> None:
    with pytest.raises(ValidationError, match="database service 'dev-postgres' is disabled"):
        Config(
            services={"cloud": False},
            projects={"entries": [_project("demo").model_copy(update={"database_service": "dev-postgres"})]},
        )


def test_project_database_provider_must_be_declared_by_a_service_manifest() -> None:
    with pytest.raises(ValidationError, match="service 'caddy' is not a managed project database provider"):
        Config(projects={"entries": [_project("demo").model_copy(update={"database_service": "caddy"})]})


def test_project_database_provider_must_exist() -> None:
    with pytest.raises(ValidationError, match="unknown database service 'missing-db'"):
        Config(projects={"entries": [_project("demo").model_copy(update={"database_service": "missing-db"})]})
