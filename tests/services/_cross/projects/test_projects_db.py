from __future__ import annotations

from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig
from toolkit.core.projects.database import (
    project_database_env_pairs,
    project_postgres_database,
    project_postgres_user,
    sanitize_postgres_identifier,
)

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _project(subdomain: str, *, with_database: bool = False) -> ProjectEntry:
    return ProjectEntry(
        subdomain=subdomain,
        auth_mode="forward_auth",
        exposure="private",
        docker_image=PINNED_IMAGE,
        placement="apps",
        database_service="dev-postgres" if with_database else "",
    )


def test_project_database_identifiers_are_deterministic() -> None:
    entry = _project("my-blog", with_database=True)

    assert project_postgres_user(entry) == "my_blog"
    assert project_postgres_database(entry) == "my_blog"
    assert sanitize_postgres_identifier("123.example") == "prj_123_example"


def test_project_db_env_pairs_include_only_enabled_tenants() -> None:
    cfg = Config(
        projects=ProjectsConfig(
            entries=[
                _project("blog", with_database=True),
                _project("gallery"),
                _project("my-app", with_database=True),
            ]
        )
    )

    assert project_database_env_pairs(cfg, "dev-postgres") == [
        ("blog", "BLOG_POSTGRES_PASSWORD"),
        ("my_app", "MY_APP_POSTGRES_PASSWORD"),
    ]
