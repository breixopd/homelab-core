"""Service discovery exposes every declared plugin and its custom behavior."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError


def _import_canonical():
    """Import the module the deploy workflow uses to discover plugins."""
    # After P0, the deploy workflow imports from toolkit.services (canonical).
    return importlib.import_module("toolkit.services")


def test_canonical_loader_exposes_plugin_entrypoints():
    """The loader the deploy workflow uses must surface the 51 flat plugins."""
    mod = _import_canonical()
    # The deploy workflow's entrypoint is enabled_service_plugins / load_service_plugins.
    # After P0 both exist on toolkit.services and return the flat instances.
    assert hasattr(mod, "enabled_service_plugins"), (
        "toolkit.services must expose enabled_service_plugins (the deploy entrypoint)"
    )
    assert hasattr(mod, "load_service_plugins"), (
        "toolkit.services must expose load_service_plugins (category-scoped access)"
    )


def test_canonical_loader_returns_grafana_with_overrides():
    """Grafana must come from the flat plugin (which declares oidc_client,
    verify, env_vars, and compose_service)."""
    mod = _import_canonical()
    # Reload to apply any inline changes (the flat loader caches).
    if hasattr(mod, "_reset_cache"):
        mod._reset_cache()
    plugin = mod.get_service_plugin("grafana")
    assert plugin is not None, "grafana plugin not discovered"
    # The flat grafana plugin declares an oidc_client property that returns a
    # real OIDCClient.
    oidc = getattr(plugin, "oidc_client", None)
    assert oidc is not None, "grafana plugin must declare a real OIDC client"
    assert getattr(oidc, "client_id", "") == "grafana"


def test_canonical_loader_includes_phase_n_additions():
    """The service catalog includes monitoring, security, and ML plugins."""
    mod = _import_canonical()
    if hasattr(mod, "_reset_cache"):
        mod._reset_cache()
    for name in ("uptime-kuma", "crowdsec", "immich-machine-learning"):
        assert mod.get_service_plugin(name) is not None, f"{name} was not discovered by the canonical service loader"


def test_category_plugins_are_dependency_and_priority_ordered():
    mod = _import_canonical()
    mod._reset_cache()

    names = list(mod.load_service_plugins("management"))

    assert names.index("postgres") < names.index("lldap")
    assert names.index("postgres") < names.index("komodo-core")
    assert names.index("authelia") < names.index("caddy")


def test_canonical_loader_flat_gpu_adaptive_immich_present():
    """The GPU-adaptive immich-machine-learning plugin's compose_service
    override must be discoverable (it queries load_capabilities)."""
    mod = _import_canonical()
    if hasattr(mod, "_reset_cache"):
        mod._reset_cache()
    plugin = mod.get_service_plugin("immich-machine-learning")
    assert plugin is not None
    # The flat plugin overrides compose_service (the base ServicePlugin doesn't
    # declare it); the override reads service.yaml + load_capabilities. Verify
    # the method is defined on the subclass (not inherited from the base).
    assert "compose_service" in type(plugin).__dict__, (
        "immich-machine-learning.compose_service override is missing — the flat GPU-adaptive plugin didn't load."
    )


def test_service_yaml_rejects_invalid_category_identifier():
    from toolkit.core.manifest.schema import ServiceManifest

    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(
            {
                "name": "broken",
                "label": "Broken",
                "description": "Broken service",
                "icon": "box",
                "category": "Bad Category",
                "placement": "control",
                "priority": 50,
            }
        )


def test_service_yaml_loader_fails_loud_on_invalid_metadata(tmp_path):
    mod = _import_canonical()
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "service.yaml").write_text(
        "name: broken\ncategory: managemnt\nplacement: control\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        mod._load_service_yaml(plugin_dir)


def test_management_contract_requires_declared_actions_to_be_implemented() -> None:
    mod = _import_canonical()

    class MissingActionPlugin(mod.ServicePlugin):
        service = "missing-action"

    plugin = MissingActionPlugin()
    plugin._yaml_data = {
        "name": "missing-action",
        "label": "Missing action",
        "description": "Invalid test plugin",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
        "management": {"actions": [{"id": "reconcile", "label": "Reconcile"}]},
    }

    with pytest.raises(ValueError, match="declares actions without implementations: reconcile"):
        mod._validate_management_contract(plugin)


def test_management_contract_rejects_undeclared_action_implementations() -> None:
    mod = _import_canonical()

    class UndeclaredActionPlugin(mod.ServicePlugin):
        service = "undeclared-action"

        def supported_actions(self) -> frozenset[str]:
            return frozenset({"reconcile"})

    plugin = UndeclaredActionPlugin()
    plugin._yaml_data = {
        "name": "undeclared-action",
        "label": "Undeclared action",
        "description": "Invalid test plugin",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
    }

    with pytest.raises(ValueError, match="implements undeclared actions: reconcile"):
        mod._validate_management_contract(plugin)


@pytest.mark.parametrize(
    ("management", "message"),
    [
        (
            {"metrics": [{"key": "queue_depth", "label": "Queue depth", "source": "status", "field": "queue"}]},
            "declares status metrics but does not implement status",
        ),
        (
            {"resources": [{"key": "queues", "label": "Queues", "columns": [{"key": "name", "label": "Name"}]}]},
            "declares resources but does not implement resources",
        ),
    ],
)
def test_management_contract_requires_status_collectors(management: dict, message: str) -> None:
    mod = _import_canonical()

    class MissingCollectorPlugin(mod.ServicePlugin):
        service = "missing-collector"

    plugin = MissingCollectorPlugin()
    plugin._yaml_data = {
        "name": "missing-collector",
        "label": "Missing collector",
        "description": "Invalid test plugin",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
        "management": management,
    }

    with pytest.raises(ValueError, match=message):
        mod._validate_management_contract(plugin)


def test_generated_artifact_contract_requires_matching_plugin_implementation() -> None:
    mod = _import_canonical()

    class MissingGeneratorPlugin(mod.ServicePlugin):
        service = "missing-generator"

    plugin = MissingGeneratorPlugin()
    plugin._yaml_data = {
        "name": "missing-generator",
        "label": "Missing generator",
        "description": "Invalid test plugin",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
        "generated_artifacts": [{"path": "generated/example.conf"}],
    }

    with pytest.raises(ValueError, match="declares generated artifacts without an implementation"):
        mod._validate_generated_artifact_contract(plugin)


def test_generated_artifact_contract_rejects_undeclared_generator() -> None:
    mod = _import_canonical()

    class UndeclaredGeneratorPlugin(mod.ServicePlugin):
        service = "undeclared-generator"

        def generate_artifacts(self, context) -> None:
            pass

    plugin = UndeclaredGeneratorPlugin()
    plugin._yaml_data = {
        "name": "undeclared-generator",
        "label": "Undeclared generator",
        "description": "Invalid test plugin",
        "icon": "box",
        "category": "management",
        "placement": "control",
        "priority": 50,
    }

    with pytest.raises(ValueError, match="implements undeclared generated artifacts"):
        mod._validate_generated_artifact_contract(plugin)


def test_discovery_fails_when_a_plugin_cannot_be_imported(tmp_path: Path, monkeypatch) -> None:
    mod = _import_canonical()
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("raise RuntimeError('broken import')\n", encoding="utf-8")
    (plugin_dir / "service.yaml").write_text(
        "name: broken\nlabel: Broken\ndescription: Broken plugin\nicon: box\n"
        "category: management\nplacement: control\npriority: 50\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_SERVICES_DIR", tmp_path)
    mod._reset_cache()

    with pytest.raises(RuntimeError, match="failed to import service plugin 'broken'"):
        mod.discover_service_plugins()

    mod._reset_cache()
