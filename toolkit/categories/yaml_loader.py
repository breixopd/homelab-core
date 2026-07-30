"""YAML category lifecycle loader.

Reads a ``category.yaml`` file and returns a complete ``Category`` object with
placement, dependency, profile, and validation metadata.

Web routes are owned exclusively by strict service manifests.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from toolkit.categories import AccessGroup, Category
from toolkit.categories.schema import CategoryManifest


def load_category_yaml(name: str) -> CategoryManifest | None:
    """Load and validate one category manifest."""
    import yaml

    yaml_path = Path(__file__).parent / name / "category.yaml"
    if not yaml_path.is_file():
        return None
    with open(yaml_path) as f:
        raw = yaml.safe_load(f) or {}
    manifest = CategoryManifest.model_validate(raw)
    if manifest.name != name:
        raise ValueError(f"category manifest name {manifest.name!r} must match directory {name!r}")
    return manifest


def load_category_from_yaml(name: str) -> Category | None:
    """Load a complete category plugin from its strict manifest.

    Optional behavior is imported only from the category's own plugin module.
    """
    data = load_category_yaml(name)
    if not data:
        return None

    validate_fn = None
    validate_name = data.validation_callback
    if validate_name:
        validate_fn = _category_callback(name, validate_name)
        if validate_fn is None:
            raise ValueError(f"category {name!r} references unknown validation callback {validate_name!r}")

    profiles_fn = None
    profiles_hook = data.selected_compose_profiles
    if profiles_hook:
        profiles_fn = _category_callback(name, profiles_hook)
        if profiles_fn is None:
            raise ValueError(f"category {name!r} references unknown profile callback {profiles_hook!r}")

    return Category(
        name=data.name,
        label=data.label,
        placement=data.placement,
        priority=data.priority,
        always_on=data.always_on,
        description=data.description,
        compose_file=data.compose_file,
        compose_profiles=list(data.compose_profiles or (name,)),
        service_group=data.service_group,
        access_group=(
            AccessGroup(
                name=data.access_group.name,
                label=data.access_group.label,
                description=data.access_group.description,
                default_invite=data.access_group.default_invite,
                administrator=data.access_group.administrator,
            )
            if data.access_group is not None
            else None
        ),
        _depends_on=list(data.depends_on),
        _validate=validate_fn,
        _selected_compose_profiles=profiles_fn,
    )


def _category_callback(category: str, callback: str):
    module_name = f"toolkit.categories.{category}.plugin"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in {module_name, module_name.rsplit(".", 1)[0]}:
            raise
        return None
    function = getattr(module, callback, None)
    return function if callable(function) else None


def discover_categories() -> list[Category]:
    """Auto-discover all category.yaml files and return Category instances."""
    from pathlib import Path

    categories: list[Category] = []
    cats_dir = Path(__file__).parent
    for child in sorted(cats_dir.iterdir()):
        if child.is_dir():
            yaml_path = child / "category.yaml"
            if yaml_path.is_file():
                category = load_category_from_yaml(child.name)
                if category is not None:
                    categories.append(category)
    return categories
