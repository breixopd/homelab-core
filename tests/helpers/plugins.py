"""Spec-based loader for service plugins in dash-named directories.

Service plugin directories use kebab-case names (e.g. ``media-cache``,
``immich-server``, ``registry-mirror``), which are not valid Python
identifiers — so ``import toolkit.services.media-cache.plugin`` fails.
The discovery loader uses ``importlib.util.spec_from_file_location`` to
work around this; tests that need a direct handle on a plugin module
function use this helper to do the same.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "toolkit" / "services"

_LOADED: dict[str, object] = {}


def load_plugin(service: str):
    """Return the imported module for ``toolkit/services/<service>/plugin.py``.

    Cached by service name so repeated calls in the same process return the
    same module object (matching the production discovery loader's caching).
    """
    if service in _LOADED:
        return _LOADED[service]
    plugin_file = _PLUGIN_DIR / service / "plugin.py"
    if not plugin_file.exists():
        raise FileNotFoundError(f"no plugin.py for service {service!r} at {plugin_file}")
    module_name = f"toolkit.services.{service}.plugin"
    module = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location(module_name, plugin_file))
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    _LOADED[service] = module
    return module
