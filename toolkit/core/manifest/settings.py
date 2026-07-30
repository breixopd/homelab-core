"""Typed access to configuration values owned by service manifests."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ServiceSettingScalar
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest, ServiceSetting


class ServiceSettingError(ValueError):
    pass


def _definition(manifest: ServiceManifest, key: str) -> ServiceSetting:
    setting = next((candidate for candidate in manifest.management.settings if candidate.key == key), None)
    if setting is None:
        raise ServiceSettingError(f"service {manifest.name!r} does not declare setting {key!r}")
    return setting


def validate_setting_value(setting: ServiceSetting, value: object) -> ServiceSettingScalar:
    if setting.type == "boolean":
        if not isinstance(value, bool):
            raise ServiceSettingError(f"setting {setting.key!r} requires a boolean value")
        return value
    if setting.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ServiceSettingError(f"setting {setting.key!r} requires a numeric value")
        if isinstance(value, float) and not math.isfinite(value):
            raise ServiceSettingError(f"setting {setting.key!r} requires a finite numeric value")
        if setting.minimum is not None and value < setting.minimum:
            raise ServiceSettingError(f"setting {setting.key!r} is below its minimum")
        if setting.maximum is not None and value > setting.maximum:
            raise ServiceSettingError(f"setting {setting.key!r} is above its maximum")
        return value
    if not isinstance(value, str):
        raise ServiceSettingError(f"setting {setting.key!r} requires a text value")
    if setting.type == "select" and value not in setting.choices:
        raise ServiceSettingError(f"setting {setting.key!r} is not an allowed choice")
    return value


def service_setting_value(cfg: Config, manifest: ServiceManifest, key: str) -> ServiceSettingScalar:
    setting = _definition(manifest, key)
    value = cfg.service_settings.get(manifest.name, {}).get(key, setting.default)
    return validate_setting_value(setting, value)


def service_setting(cfg: Config, service: str, key: str) -> ServiceSettingScalar:
    from toolkit.core.manifest.catalog import load_service_catalog

    return service_setting_value(cfg, load_service_catalog().require(service), key)


def service_setting_bool(cfg: Config, service: str, key: str) -> bool:
    value = service_setting(cfg, service, key)
    if not isinstance(value, bool):
        raise ServiceSettingError(f"service setting {service}.{key} requires a boolean value")
    return value


def service_setting_str(cfg: Config, service: str, key: str) -> str:
    value = service_setting(cfg, service, key)
    if not isinstance(value, str):
        raise ServiceSettingError(f"service setting {service}.{key} requires text")
    return value


def service_setting_int(cfg: Config, service: str, key: str) -> int:
    value = service_setting(cfg, service, key)
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
        raise ServiceSettingError(f"service setting {service}.{key} requires an integer")
    return int(value)


def service_enabled(cfg: Config, service: str) -> bool:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    return service_is_enabled(cfg, load_service_catalog().require(service))


def validate_service_setting_overrides(cfg: Config, catalog: ServiceCatalog | None = None) -> None:
    if not cfg.service_settings:
        return
    if catalog is None:
        from toolkit.core.manifest.catalog import load_service_catalog

        catalog = load_service_catalog()
    manifests = {manifest.name: manifest for manifest in catalog.manifests}
    for service, values in cfg.service_settings.items():
        manifest = manifests.get(service)
        if manifest is None:
            raise ServiceSettingError(f"configuration references unknown service {service!r}")
        declared = {setting.key: setting for setting in manifest.management.settings}
        unknown = sorted(set(values) - set(declared))
        if unknown:
            raise ServiceSettingError(
                f"configuration for {service!r} contains undeclared settings: {', '.join(unknown)}"
            )
        for key, value in values.items():
            validate_setting_value(declared[key], value)
