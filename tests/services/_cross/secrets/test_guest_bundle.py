from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.config.config import Config, ProjectEntry, ProjectsConfig, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.guest_bundle import assert_guest_bundle_safe, render_guest_bundle, required_role_environment


def test_apps_bundle_excludes_controller_and_media_secrets(tmp_path: Path):
    services = tmp_path / "toolkit" / "services" / "immich-server"
    services.mkdir(parents=True)
    (services / "compose.yaml").write_text("DB_PASSWORD: ${IMMICH_DB_PASSWORD}\n")
    # The service metadata loader resolves the installed repository manifests;
    # verify filtering independently with a role that has no local references.
    role_env = tmp_path / "generated" / "apps" / ".env"
    role_env.parent.mkdir(parents=True)
    role_env.write_text(
        "COMPOSE_PROFILES=cloud\nPROXMOX_API_TOKEN_ID=controller\nIMMICH_DB_PASSWORD=apps\nSONARR_API_KEY=media\n",
        encoding="utf-8",
    )
    bundle = render_guest_bundle(tmp_path, "apps")
    assert bundle == tmp_path / "generated" / "bundles" / "apps" / ".hooks.env"
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert bundle.parent.stat().st_mode & 0o777 == 0o700
    content = bundle.read_text()
    assert "IMMICH_DB_PASSWORD=apps" in content
    assert "SONARR_API_KEY" not in content
    assert "PROXMOX_API_TOKEN_ID" not in content
    assert "CF_API_TOKEN" not in content
    assert_guest_bundle_safe(bundle)


def test_bundle_uses_compiled_role_model_as_secret_boundary(tmp_path: Path) -> None:
    model = tmp_path / "generated" / "apps" / "compose.yaml"
    model.parent.mkdir(parents=True)
    model.write_text("services:\n  app:\n    environment:\n      TOKEN: ${APP_TOKEN}\n", encoding="utf-8")
    (model.parent / ".env").write_text(
        "APP_TOKEN=selected\nMEDIA_TOKEN=excluded\nCOMPOSE_PROFILES=app\n",
        encoding="utf-8",
    )

    bundle = render_guest_bundle(tmp_path, "apps")

    assert bundle.read_text(encoding="utf-8") == "APP_TOKEN=selected\nCOMPOSE_PROFILES=app\n"


def test_registry_pull_token_never_enters_guest_bundle(tmp_path: Path) -> None:
    model = tmp_path / "generated" / "infra" / "compose.yaml"
    model.parent.mkdir(parents=True)
    model.write_text("services:\n  app:\n    environment:\n      TOKEN: ${APP_TOKEN}\n", encoding="utf-8")
    (model.parent / ".env").write_text(
        "APP_TOKEN=selected\nGHCR_READ_TOKEN=controller-only\n",
        encoding="utf-8",
    )

    bundle = render_guest_bundle(tmp_path, "infra")

    assert "APP_TOKEN=selected" in bundle.read_text(encoding="utf-8")
    assert "GHCR_READ_TOKEN" not in bundle.read_text(encoding="utf-8")


def test_bundle_restores_authoritative_plaintext_after_compose_transformation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_TEST_PLAINTEXT_SECRETS", "1")
    model = tmp_path / "generated" / "apps" / "compose.yaml"
    model.parent.mkdir(parents=True)
    model.write_text(
        "services:\n  vault:\n    environment:\n      ADMIN_TOKEN: ${VAULTWARDEN_ADMIN_TOKEN}\n",
        encoding="utf-8",
    )
    (model.parent / ".env").write_text(
        "VAULTWARDEN_ADMIN_TOKEN=$argon2id$compose-only-hash\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.enc.yaml").write_text(
        yaml.safe_dump({"VAULTWARDEN_ADMIN_TOKEN": "hook-plaintext"}),
        encoding="utf-8",
    )

    bundle = render_guest_bundle(tmp_path, "apps")

    assert bundle.read_text(encoding="utf-8") == "VAULTWARDEN_ADMIN_TOKEN='hook-plaintext'\n"


def test_repository_role_boundaries_include_manifest_bootstrap_credentials(tmp_path: Path) -> None:
    save_config(Config(), tmp_path)

    infra = required_role_environment(tmp_path, "infra")
    media = required_role_environment(tmp_path, "media")
    apps = required_role_environment(tmp_path, "apps")

    assert "SSO_USER_PASSWORD" in infra
    assert "CF_API_TOKEN" in infra
    assert "SONARR_API_KEY" in media
    assert "JELLYFIN_ADMIN_PASSWORD" in media
    assert "FMD_REGISTRATION_TOKEN" in apps
    assert "VAULTWARDEN_ADMIN_TOKEN" in apps
    assert "PROXMOX_API_TOKEN_SECRET" not in infra | media | apps


def test_service_database_password_is_scoped_to_consumer_and_provider_nodes(tmp_path: Path) -> None:
    services = tmp_path / "toolkit" / "services"
    provider = services / "database"
    consumer = services / "application"
    provider.mkdir(parents=True)
    consumer.mkdir(parents=True)
    (provider / "service.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "database",
                "label": "Database",
                "description": "Shared database",
                "icon": "database",
                "category": "cloud",
                "placement": "control",
                "priority": 10,
                "service_endpoint": {"container_port": 5432, "published_port": 5432},
                "variables": {"DATABASE_USER": "admin", "DATABASE_NAME": "postgres"},
                "required_secrets": [
                    {
                        "name": "DATABASE_PASSWORD",
                        "tier": "generated",
                        "description": "administrator password",
                        "rotation": "persistent",
                    }
                ],
                "database_provider": {
                    "engine": "postgresql",
                    "admin_username_env": "DATABASE_USER",
                    "admin_password_env": "DATABASE_PASSWORD",
                    "admin_database_env": "DATABASE_NAME",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (consumer / "service.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "application",
                "label": "Application",
                "description": "Database consumer",
                "icon": "box",
                "category": "cloud",
                "placement": "apps",
                "priority": 20,
                "depends_on": ["database"],
                "required_secrets": [
                    {
                        "name": "APPLICATION_DB_PASSWORD",
                        "tier": "generated",
                        "description": "application database password",
                        "rotation": "reconcile",
                    }
                ],
                "databases": [
                    {
                        "provider": "database",
                        "database": "application",
                        "username": "application",
                        "host_env": "APPLICATION_DB_HOST",
                        "port_env": "APPLICATION_DB_PORT",
                        "database_env": "APPLICATION_DB_NAME",
                        "username_env": "APPLICATION_DB_USER",
                        "password_env": "APPLICATION_DB_PASSWORD",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    logging = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    (provider / "compose.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "database": {
                        "image": "postgres:16@sha256:" + "a" * 64,
                        "environment": {
                            "POSTGRES_USER": "${DATABASE_USER}",
                            "POSTGRES_PASSWORD": "${DATABASE_PASSWORD}",
                            "POSTGRES_DB": "${DATABASE_NAME}",
                        },
                        "ports": ["${PRIVATE_IP}:5432:5432"],
                        "logging": logging,
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (consumer / "compose.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "application": {
                        "image": "example:1@sha256:" + "b" * 64,
                        "environment": {"DATABASE_PASSWORD": "${APPLICATION_DB_PASSWORD}"},
                        "logging": logging,
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for node in ("infra", "apps", "media"):
        model = tmp_path / "generated" / node / "compose.yaml"
        model.parent.mkdir(parents=True)
        model.write_text("services: {}\n", encoding="utf-8")

    cfg = Config(services={"cloud": True})
    infra = required_role_environment(tmp_path, "infra", cfg)
    apps = required_role_environment(tmp_path, "apps", cfg)
    media = required_role_environment(tmp_path, "media", cfg)

    assert "APPLICATION_DB_PASSWORD" in infra
    assert "APPLICATION_DB_PASSWORD" in apps
    assert "APPLICATION_DB_PASSWORD" not in media


def test_project_database_secret_is_scoped_to_project_and_provider_nodes(tmp_path: Path) -> None:
    cfg = Config(
        projects=ProjectsConfig(
            entries=[
                ProjectEntry(
                    name="Status",
                    subdomain="status",
                    auth_mode="forward_auth",
                    exposure="private",
                    docker_image="docker.io/library/nginx:1@sha256:" + "a" * 64,
                    placement="media",
                    database_service="dev-postgres",
                )
            ]
        ),
    )
    save_config(cfg, config_path(tmp_path))

    assert "STATUS_POSTGRES_PASSWORD" in required_role_environment(tmp_path, "media")
    assert "STATUS_POSTGRES_PASSWORD" in required_role_environment(tmp_path, "apps")
    assert "STATUS_POSTGRES_PASSWORD" not in required_role_environment(tmp_path, "infra")
