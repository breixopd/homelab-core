from pathlib import Path

from toolkit.core.config.config import load_config
from toolkit.core.config.storage import config_path
from toolkit.services import ServicePlugin, enabled_service_plugins


def test_every_enabled_service_owns_functional_verification() -> None:
    root = Path(__file__).parents[3]
    cfg = load_config(config_path(root))

    missing = sorted(
        plugin.service
        for _category, plugin in enabled_service_plugins(cfg)
        if type(plugin).verify is ServicePlugin.verify
    )

    assert missing == []
