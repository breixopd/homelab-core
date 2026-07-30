from __future__ import annotations

from typing import TYPE_CHECKING

from toolkit.categories import Category

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

_REGISTRY: dict[str, Category] = {}
_LOADED = False


def get_category(name: str) -> Category:
    return _REGISTRY[name]


def all_categories() -> list[Category]:
    return list(_REGISTRY.values())


def enabled_categories(config: Config) -> list[Category]:
    return [cat for cat in _REGISTRY.values() if config.category_enabled(cat.name)]


def dependency_sort(categories: list[Category]) -> list[Category]:
    """Topological sort by depends_on."""
    by_name = {c.name: c for c in categories}
    visited: set[str] = set()
    visiting: set[str] = set()
    result: list[Category] = []

    def visit(name: str):
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"category dependency cycle includes {name!r}")
        cat = by_name.get(name)
        if cat is None:
            raise ValueError(f"enabled category depends on unavailable category {name!r}")
        visiting.add(name)
        for dep in cat.depends_on():
            visit(dep)
        visiting.remove(name)
        visited.add(name)
        result.append(cat)

    for c in categories:
        visit(c.name)
    return result


def load_all():
    """Auto-discover category.yaml files and register them."""
    global _LOADED
    if _LOADED:
        return
    from toolkit.categories.yaml_loader import discover_categories

    discovered = discover_categories()
    names = [category.name for category in discovered]
    if len(names) != len(set(names)):
        raise ValueError("duplicate discovered category plugins")
    known = set(names)
    access_groups = [category.access_group for category in discovered if category.access_group is not None]
    access_group_names = [group.name for group in access_groups]
    if len(access_group_names) != len(set(access_group_names)):
        raise ValueError("duplicate access groups declared by category plugins")
    if sum(group.administrator for group in access_groups) != 1:
        raise ValueError("category plugins must declare exactly one administrator access group")
    known_access_groups = set(access_group_names)
    for category in discovered:
        unknown = sorted(set(category.depends_on()) - known)
        if unknown:
            raise ValueError(f"category {category.name!r} depends on unknown categories: {', '.join(unknown)}")
        if category.service_group and category.service_group not in known_access_groups:
            raise ValueError(f"category {category.name!r} references unknown service group {category.service_group!r}")
    dependency_sort(discovered)
    for category in discovered:
        if category.name not in _REGISTRY:
            _REGISTRY[category.name] = category
    _LOADED = True
