"""Shared first-run contracts discovered from service manifests."""

from __future__ import annotations

from collections.abc import Mapping

from toolkit.core.config.config import Config, ServicesConfig, ServiceSettingScalar
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.routes import predicate_matches, service_is_enabled
from toolkit.core.manifest.schema import RequiredSecretManifest, ServiceManifest, ServiceSetting
from toolkit.core.manifest.settings import validate_setting_value


def setup_setting_definitions(
    catalog: ServiceCatalog | None = None,
) -> tuple[tuple[ServiceManifest, ServiceSetting], ...]:
    selected = catalog or load_service_catalog()
    return tuple(
        (manifest, setting)
        for manifest in selected.manifests
        for setting in manifest.management.settings
        if setting.setup
    )


def setup_setting_environment_name(service: str, key: str) -> str:
    owner = service.upper().replace("-", "_")
    setting = key.upper().replace("-", "_")
    return f"HOMELAB_SETTING_{owner}_{setting}"


def setup_secret_environment_name(name: str) -> str:
    return f"HOMELAB_SECRET_{name}"


def parse_setup_setting(setting: ServiceSetting, raw: str) -> ServiceSettingScalar:
    value: object = raw.strip()
    if setting.type == "boolean":
        normalized = str(value).lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{setting.key} requires true or false")
        value = normalized in {"1", "true", "yes", "on"}
    elif setting.type == "number":
        number = float(str(value))
        value = int(number) if number.is_integer() else number
    return validate_setting_value(setting, value)


def setup_settings_from_environment(
    services: ServicesConfig,
    environment: Mapping[str, str],
    catalog: ServiceCatalog | None = None,
) -> dict[str, dict[str, ServiceSettingScalar]]:
    enabled_categories = services.model_dump(mode="python")
    values: dict[str, dict[str, ServiceSettingScalar]] = {}
    for manifest, setting in setup_setting_definitions(catalog):
        if not enabled_categories.get(manifest.category, False):
            continue
        name = setup_setting_environment_name(manifest.name, setting.key)
        raw = environment.get(name)
        if raw is None:
            continue
        values.setdefault(manifest.name, {})[setting.key] = parse_setup_setting(setting, raw)
    return values


def active_setup_secrets(
    config: Config,
    catalog: ServiceCatalog | None = None,
) -> dict[str, tuple[ServiceManifest, RequiredSecretManifest]]:
    selected = catalog or load_service_catalog()
    active: dict[str, tuple[ServiceManifest, RequiredSecretManifest]] = {}
    for manifest in selected.manifests:
        if not service_is_enabled(config, manifest, selected):
            continue
        for secret in manifest.required_secrets:
            if secret.setup is None or not all(
                predicate_matches(config, predicate, selected) for predicate in secret.setup.when
            ):
                continue
            active[secret.name] = (manifest, secret)
    return active


def prepare_bootstrap_credentials(config: Config, credentials: Mapping[str, str]) -> dict[str, str]:
    from toolkit.services import enabled_service_plugins

    values = dict(credentials)
    for _, plugin in enabled_service_plugins(config):
        updates = plugin.prepare_bootstrap_credentials(config, dict(values))
        owned_names = {secret.name for secret in plugin.manifest.required_secrets}
        if set(updates) - owned_names:
            raise ValueError(f"{plugin.service} produced an undeclared bootstrap credential")
        values.update(updates)
    return values


def setup_credentials_from_environment(
    config: Config,
    environment: Mapping[str, str],
    catalog: ServiceCatalog | None = None,
) -> dict[str, str]:
    values = {
        name: value.strip()
        for name in active_setup_secrets(config, catalog)
        if (value := environment.get(setup_secret_environment_name(name), "")).strip()
    }
    return prepare_bootstrap_credentials(config, values)
