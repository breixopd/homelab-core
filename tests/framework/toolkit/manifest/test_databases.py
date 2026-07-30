from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.databases import CompiledDatabaseBinding, compile_database_bindings
from toolkit.core.manifest.schema import ServiceManifest


def _provider() -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "postgres",
            "label": "PostgreSQL",
            "description": "Shared database",
            "icon": "database",
            "category": "cloud",
            "placement": "apps",
            "priority": 10,
            "variables": {"POSTGRES_USER": "admin", "POSTGRES_DB": "postgres"},
            "required_secrets": [
                {
                    "name": "POSTGRES_PASSWORD",
                    "tier": "generated",
                    "description": "administrator password",
                    "rotation": "reconcile",
                }
            ],
            "database_provider": {
                "engine": "postgresql",
                "admin_username_env": "POSTGRES_USER",
                "admin_password_env": "POSTGRES_PASSWORD",
                "admin_database_env": "POSTGRES_DB",
            },
            "service_endpoint": {"container_port": 5432},
        }
    )


def _consumer(name: str, *, category: str = "cloud") -> ServiceManifest:
    password_env = f"{name.upper()}_DB_PASSWORD"
    return ServiceManifest.model_validate(
        {
            "name": name,
            "label": name.title(),
            "description": f"{name} application",
            "icon": "box",
            "category": category,
            "placement": "apps",
            "priority": 20,
            "depends_on": ["postgres"],
            "required_secrets": [
                {
                    "name": password_env,
                    "tier": "generated",
                    "description": "application database password",
                    "rotation": "reconcile",
                }
            ],
            "databases": [
                {
                    "provider": "postgres",
                    "database": name,
                    "username": name,
                    "host_env": f"{name.upper()}_DB_HOST",
                    "port_env": f"{name.upper()}_DB_PORT",
                    "database_env": f"{name.upper()}_DB_NAME",
                    "username_env": f"{name.upper()}_DB_USER",
                    "password_env": password_env,
                }
            ],
        }
    )


def test_database_bindings_are_projected_from_enabled_consumer_plugins() -> None:
    catalog = ServiceCatalog((_provider(), _consumer("gitea"), _consumer("archive", category="media")))
    cfg = Config(services={"cloud": True, "media": False})

    assert compile_database_bindings(cfg, catalog) == (
        CompiledDatabaseBinding(
            service="gitea",
            provider="postgres",
            engine="postgresql",
            database="gitea",
            username="gitea",
            host_env="GITEA_DB_HOST",
            port_env="GITEA_DB_PORT",
            database_env="GITEA_DB_NAME",
            username_env="GITEA_DB_USER",
            password_env="GITEA_DB_PASSWORD",
        ),
    )


def test_database_bindings_can_be_filtered_by_provider() -> None:
    catalog = ServiceCatalog((_provider(), _consumer("gitea")))

    assert len(compile_database_bindings(Config(), catalog, provider="postgres")) == 1
    assert compile_database_bindings(Config(), catalog, provider="other") == ()
