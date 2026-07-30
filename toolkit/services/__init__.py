"""Flat services directory — the canonical home for all self-contained service definitions.

Each service lives in ``toolkit/services/<name>/`` with:
  - ``plugin.py``     — the ServicePlugin subclass (all behavior)
  - ``service.yaml``  — declarative data (routes, dns, secrets, memory_tier, etc.)
  - ``compose.yaml``  — the docker-compose service block

The framework discovers services by scanning this directory. Adding a service
= drop 3 files here + enable in config.yaml. No framework code changes.

``ServicePlugin`` (the base class) + ``OIDCClient`` are defined here so the
plugin contract + loader live together. The deploy workflow, verify hooks, and
watchdog all import loader functions (``enabled_service_plugins``,
``load_service_plugins``, ``get_service_plugin``, ``all_service_plugins``) from
this module.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.core.manifest.schema import ServiceManagement, ServiceManifest
    from toolkit.core.ops.dump_repository import DumpRecord
    from toolkit.core.ops.hook_verify import VerifyCheck

_SERVICES_DIR = Path(__file__).parent


@dataclass
class OIDCClient:
    """OIDC client registration for a service (declared in YAML or code)."""

    client_id: str
    redirect_uris: list[str] = field(default_factory=list)
    secret_env_var: str = ""  # env var name holding the client secret
    native: bool = False  # True = service does its own OIDC (no forward-auth)


@dataclass(frozen=True, slots=True)
class IdentityProvisionResult:
    key: str
    status: Literal["completed", "pending", "skipped", "warning", "failed"]
    message: str


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentContext:
    """Inputs available to one plugin's declared runtime-variable compiler."""

    config: Config
    node: str
    root: Path | None
    secrets: Mapping[str, str]
    previous: Mapping[str, str]


class RuntimeLifecycleContext(Protocol):
    """Bounded deploy operations available to service-owned runtime hooks."""

    root: Path
    node: str

    def compose(self, *args: str) -> Any: ...

    def run_host(self, args: list[str]) -> Any: ...

    def services_healthy(self, services: tuple[str, ...]) -> bool: ...

    def wait_until_healthy(self, name: str, services: tuple[str, ...]) -> bool: ...

    def retry_services(self, services: tuple[str, ...]) -> bool: ...

    def run_recovery(self, function: str, module: str, **kwargs: Any) -> None: ...

    def record_failure(self) -> None: ...

    def resolve_failure(self) -> None: ...

    def log(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def environment(self, name: str, default: str = "") -> str: ...

    def state(self, key: str, default: Any = None) -> Any: ...

    def set_state(self, key: str, value: Any) -> None: ...

    def add_compose_up_option(self, option: str) -> None: ...

    def remove_compose_up_option(self, option: str) -> None: ...


@dataclass(frozen=True, slots=True)
class FleetOnboardingContribution:
    """Service-owned variables and progress messages for one fleet onboarding."""

    variables: Mapping[str, object] = field(default_factory=dict)
    logs: tuple[str, ...] = ()


class FleetOnboardingContext(Protocol):
    """Bounded operations available after the primary fleet playbook completes."""

    config: Config
    host: ExternalHost
    root: Path
    variables: Mapping[str, object]

    def log(self, message: str) -> None: ...

    def retry_integrations(self, integrations: tuple[str, ...]) -> bool: ...


class ServicePlugin:
    """Base class for service plugins. Override ``post_start`` + ``verify``.

    The ``service`` attribute must match the ``name:`` field in ``service.yaml``.
    ``category``, ``placement``, and ``icon`` are auto-set from ``service.yaml`` by the
    discovery loader.

    Default ``secrets_needed`` / ``credentials`` read from ``service.yaml`` so a
    service that only needs static metadata (routes, icon, restart policy) needs
    no Python file — its ``service.yaml`` covers everything.
    """

    service: str = ""
    category: str = ""  # auto-set by the loader
    placement: str = ""  # auto-set from service.yaml
    icon: str = "🔧"  # auto-set from service.yaml
    essential: bool = False  # auto-set from service.yaml
    heal_aliases: tuple[str, ...] = ()  # extra container names routed to heal()

    # Populated by the discovery loader for default method implementations.
    def __init__(self) -> None:
        self._yaml_data: dict = {}
        self._plugin_dir: Path | None = None

    def compose_application(self) -> dict:
        """Load this plugin's standalone Compose application."""
        import yaml

        if self._plugin_dir is None:
            raise RuntimeError(f"service plugin {self.service!r} has not been discovered")
        compose_path = self._plugin_dir / "compose.yaml"
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
            raise ValueError(f"{compose_path} must contain a top-level services mapping")
        return document

    @property
    def has_compose_application(self) -> bool:
        """Return whether this plugin owns a standalone Compose model."""
        return self._plugin_dir is not None and (self._plugin_dir / "compose.yaml").is_file()

    def compose_service(self, cfg: Config | None = None) -> dict:
        """Return this manifest service's Compose block."""
        services = self.compose_application()["services"]
        block = services.get(self.service)
        if not isinstance(block, dict):
            raise ValueError(f"Compose application for {self.service!r} does not define that service")
        return deepcopy(block)

    # ── secrets ────────────────────────────────────────────────────────────────
    def secrets_needed(self) -> list:
        """Return typed secret requirements from this service's strict manifest."""
        from toolkit.core.manifest.schema import ServiceManifest
        from toolkit.core.secrets.secrets import manifest_secret_spec

        manifest = ServiceManifest.model_validate(self._yaml_data)
        return [manifest_secret_spec(entry) for entry in manifest.required_secrets]

    # ── credentials (Vaultwarden entries) ─────────────────────────────────────
    def credentials(self, cfg) -> list:
        """Return manifest-declared Vaultwarden entries for this service."""
        from toolkit.core.config.credential_catalog import CredentialEntry
        from toolkit.core.manifest.schema import ServiceManifest

        entries: list[CredentialEntry] = []
        manifest = ServiceManifest.model_validate(self._yaml_data)
        for credential in manifest.credentials:
            domain = getattr(cfg, "domain", "localhost") if cfg else "localhost"
            proto = "http" if domain == "localhost" else "https"
            url = credential.url.format(domain=domain, proto=proto)
            entries.append(
                CredentialEntry(
                    name=credential.name,
                    secret_key=credential.password_env,
                    username_key=credential.username_env,
                    url_template=url,
                    tags=credential.tags or (self.service,),
                    username=credential.username,
                    notes=credential.notes,
                    category=self.category,
                )
            )
        return entries

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Post-deploy automation (bootstrap, config, health checks). Default: no-op."""
        return []

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        """Generate every artifact declared by this service manifest."""

    def prepare_runtime_deployment(
        self,
        context: RuntimeLifecycleContext,
        services: tuple[str, ...],
    ) -> None:
        """Validate or prepare this plugin before any runtime startup wave."""

    def runtime_environment(self, context: RuntimeEnvironmentContext) -> dict[str, str]:
        """Compile manifest-declared runtime variables for this service."""
        return {}

    def reconcile_runtime_credentials(self, cfg: Config, root: Path) -> list[str]:
        """Reconcile service-owned credentials after a guest runtime is available."""
        return []

    def pre_deploy_database_dump(self, cfg: Config, root: Path, *, vm: str | None = None) -> str | None:
        """Create a provider-owned pre-deploy database dump."""
        raise NotImplementedError(f"{self.service} does not implement database dump maintenance")

    def list_database_dumps(self, cfg: Config, root: Path, *, vm: str | None = None) -> list[DumpRecord]:
        """List provider-owned pre-deploy database dumps."""
        raise NotImplementedError(f"{self.service} does not implement database dump discovery")

    def restore_database_dump(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> bool:
        """Restore one provider-owned database dump after framework validation."""
        raise NotImplementedError(f"{self.service} does not implement database restore maintenance")

    def run_database_restore_drill(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> tuple[bool, int, str]:
        """Restore and verify a provider-owned dump in an isolated runtime."""
        raise NotImplementedError(f"{self.service} does not implement database restore drills")

    def controller_access_checks(self, cfg: Config, root: Path) -> list[VerifyCheck]:
        """Return controller-originated access checks owned by this service."""
        return []

    def ansible_secret_variables(self, cfg: Config, secrets: dict[str, str]) -> dict[str, str]:
        """Return service-owned credentials for ephemeral Ansible injection."""
        return {}

    def prepare_bootstrap_credentials(self, cfg: Config, credentials: dict[str, str]) -> dict[str, str]:
        """Return service-owned derived credentials during first-run setup."""
        return {}

    def provision_identity(
        self,
        cfg: Config,
        secrets: dict[str, str],
        email: str,
        *,
        root: Path | None = None,
    ) -> tuple[IdentityProvisionResult, ...]:
        """Provision one directory identity into this service."""
        return ()

    def before_runtime_start(
        self,
        context: RuntimeLifecycleContext,
        services: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Adjust or reconcile this plugin immediately before its Compose wave."""
        return services

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        """Reconcile this plugin immediately after its Compose wave."""
        provider = self.manifest.database_provider
        if provider is not None and provider.engine == "postgresql":
            context.run_recovery(
                "ensure_postgres_healthy",
                "toolkit.services.sdk.postgres",
                node=context.node,
                service=self.service,
            )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Return verify checks for this service. Default: no-op."""
        return []

    def heal(self, cfg: Config, root: Path, *, service: str | None = None) -> list[str] | None:
        """Structured auto-heal beyond a plain container restart.

        Return ``None`` when this plugin has no heal logic for the request.
        Return a list of human-readable log lines when heal steps were run.

        Implementations must be idempotent and safe to call when the service is
        already healthy.
        """
        return None

    def configure_host_integrations(
        self,
        cfg: Config,
        *,
        previous: ExternalHost | None,
        current: ExternalHost | None,
    ) -> Config:
        """Apply service-owned desired-state changes for one managed-host mutation."""
        return cfg

    def reconcile_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
        *,
        selected: bool,
    ) -> list[str]:
        """Reconcile one manifest-declared controller integration for a host."""
        return []

    def host_integration_refresh_nodes(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        *,
        selected: bool,
    ) -> tuple[str, ...]:
        """Return runtime nodes that must receive a desired-state refresh.

        Host integrations can project files consumed by an already-running
        service.  The controller uses this declarative result to run the normal
        bounded deploy workflow on affected nodes; service plugins never need to
        know how transport, compose, or progress reporting work.
        """
        return ()

    def cleanup_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> list[str]:
        """Remove resources owned by one manifest-declared host integration."""
        return []

    def host_integration_ansible_variables(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> dict[str, str]:
        """Return bounded onboarding variables owned by an integration.

        Values may be sensitive. Callers must pass them only through the
        owner-only ephemeral Ansible vars-file path and tasks that consume
        secrets must use ``no_log``.
        """
        return {}

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        """Return one selected host integration's agent status, when it has one."""
        return None

    def prepare_fleet_onboarding(
        self,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> FleetOnboardingContribution:
        """Return service-owned inputs for the initial fleet onboarding playbook."""
        return FleetOnboardingContribution()

    def after_fleet_onboarding(self, context: FleetOnboardingContext) -> None:
        """Perform bounded service-owned reconciliation after fleet onboarding."""
        return None

    def selected_for_fleet_host(self, host: ExternalHost) -> bool:
        """Return whether this plugin owns an integration selected for one fleet host."""
        return any(integration.id in host.services for integration in self.manifest.host_integrations)

    def is_enabled(self, cfg: Config) -> bool:
        """Return whether this optional service is enabled by its manifest condition."""
        from toolkit.core.manifest.routes import service_is_enabled
        from toolkit.core.manifest.schema import ServiceManifest

        return service_is_enabled(cfg, ServiceManifest.model_validate(self._yaml_data))

    def runtime_node(self, cfg: Config) -> str:
        """Return the enabled machine that owns this service at runtime."""
        from toolkit.core.manifest.placement import service_node

        return service_node(cfg, self.service)

    def runtime_address(self, cfg: Config, *, local_address: str = "localhost") -> str:
        """Return a reachable service host for local or distributed execution."""
        from toolkit.core.manifest.placement import service_address

        return service_address(cfg, self.service) if cfg.is_multi_node else local_address

    def management(self) -> ServiceManagement:
        """Return this service's validated operator-management capabilities."""
        return self.manifest.management

    @property
    def manifest(self) -> ServiceManifest:
        """Return this plugin's validated declarative manifest."""
        from toolkit.core.manifest.schema import ServiceManifest

        return ServiceManifest.model_validate(self._yaml_data)

    def setting(self, cfg: Config, key: str) -> bool | int | float | str:
        """Resolve one typed service-owned setting override or manifest default."""
        from toolkit.core.manifest.settings import service_setting_value

        return service_setting_value(cfg, self.manifest, key)

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        """Return bounded scalar status candidates; the controller allow-lists declared fields."""
        return {}

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        """Return candidate rows for manifest-declared read-only resource tables."""
        return {}

    def supported_actions(self) -> frozenset[str]:
        """Return action IDs implemented by this plugin."""
        return frozenset()

    def execute_action(
        self,
        action: str,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> list[str]:
        """Execute one declared parameterless action and return operator log lines."""
        raise NotImplementedError(f"{self.service} does not implement action {action!r}")

    @property
    def oidc_client(self) -> OIDCClient | None:
        """OIDC client config for this service. Override if the service needs OIDC. Default: None."""
        return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} service={self.service!r}>"


# Cache: list[ServicePlugin] — cached after first call; cleared by tests
# that add/remove service dirs at runtime via ``_reset_cache()``.
_cache: list[ServicePlugin] | None = None


def _load_service_yaml(plugin_dir: Path) -> dict:
    """Load ``service.yaml`` from a plugin directory.

    Validates via Pydantic schema (Phase E) so typos like
    ``restart_policy: carful`` are caught at discovery time.
    """
    import logging

    import yaml

    from toolkit.core.manifest.schema import ServiceManifest

    path = plugin_dir / "service.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        manifest = ServiceManifest.model_validate(data)
        if manifest.name != plugin_dir.name:
            raise ValueError(f"manifest name {manifest.name!r} must match directory {plugin_dir.name!r}")
        return data
    except Exception:
        logging.getLogger(__name__).exception("service.yaml validation failed for %s", plugin_dir.name)
        raise


def _find_plugin_class(module) -> type[ServicePlugin] | None:
    """Find the ServicePlugin subclass in a module (the first one declared)."""
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and issubclass(attr, ServicePlugin) and attr is not ServicePlugin:
            return attr
    return None


def _validate_management_contract(plugin: ServicePlugin) -> None:
    """Fail discovery when declarative UI capabilities lack matching behavior."""
    capabilities = plugin.management()
    declared_actions = {action.id for action in capabilities.actions}
    implemented_actions = set(plugin.supported_actions())
    missing_actions = declared_actions - implemented_actions
    if missing_actions:
        raise ValueError(
            f"service plugin {plugin.service!r} declares actions without implementations: "
            f"{', '.join(sorted(missing_actions))}"
        )
    undeclared_actions = implemented_actions - declared_actions
    if undeclared_actions:
        raise ValueError(
            f"service plugin {plugin.service!r} implements undeclared actions: {', '.join(sorted(undeclared_actions))}"
        )
    if any(metric.source == "status" for metric in capabilities.metrics):
        if type(plugin).status is ServicePlugin.status:
            raise ValueError(f"service plugin {plugin.service!r} declares status metrics but does not implement status")
    if capabilities.resources and type(plugin).resources is ServicePlugin.resources:
        raise ValueError(f"service plugin {plugin.service!r} declares resources but does not implement resources")


def _validate_generated_artifact_contract(plugin: ServicePlugin) -> None:
    declared = bool(plugin.manifest.generated_artifacts)
    implemented = type(plugin).generate_artifacts is not ServicePlugin.generate_artifacts
    if declared and not implemented:
        raise ValueError(f"service plugin {plugin.service!r} declares generated artifacts without an implementation")
    if implemented and not declared:
        raise ValueError(f"service plugin {plugin.service!r} implements undeclared generated artifacts")


def _validate_runtime_environment_contract(plugin: ServicePlugin) -> None:
    declared = bool(plugin.manifest.runtime_variables)
    implemented = type(plugin).runtime_environment is not ServicePlugin.runtime_environment
    if declared and not implemented:
        raise ValueError(f"service plugin {plugin.service!r} declares runtime variables without an implementation")
    if implemented and not declared:
        raise ValueError(f"service plugin {plugin.service!r} implements undeclared runtime variables")


def _validate_identity_contract(plugin: ServicePlugin) -> None:
    declared = any(entry.mode == "plugin" for entry in plugin.manifest.identity.provisioning)
    implemented = type(plugin).provision_identity is not ServicePlugin.provision_identity
    if declared and not implemented:
        raise ValueError(f"service plugin {plugin.service!r} declares identity provisioning without an implementation")
    if implemented and not declared:
        raise ValueError(f"service plugin {plugin.service!r} implements undeclared identity provisioning")


def _validate_host_integration_contract(plugin: ServicePlugin) -> None:
    integrations = plugin.manifest.host_integrations
    lifecycle_declared = any(integration.controller_lifecycle for integration in integrations)
    reconcile_implemented = type(plugin).reconcile_host_integration is not ServicePlugin.reconcile_host_integration
    cleanup_implemented = type(plugin).cleanup_host_integration is not ServicePlugin.cleanup_host_integration
    if lifecycle_declared and not (reconcile_implemented and cleanup_implemented):
        raise ValueError(
            f"service plugin {plugin.service!r} declares a host controller lifecycle "
            "without reconcile and cleanup hooks"
        )
    if (reconcile_implemented or cleanup_implemented) and not lifecycle_declared:
        raise ValueError(f"service plugin {plugin.service!r} implements an undeclared host controller lifecycle")
    host_hook_names = (
        "configure_host_integrations",
        "host_integration_ansible_variables",
        "host_integration_status",
    )
    implements_host_hook = any(
        getattr(type(plugin), name) is not getattr(ServicePlugin, name) for name in host_hook_names
    )
    if not integrations and implements_host_hook:
        raise ValueError(f"service plugin {plugin.service!r} implements host hooks without a host integration")


def _import_plugin_module(module_name: str, plugin_file: Path):
    """Import a plugin package, falling back only when its package path is absent."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        parent_name = module_name.rsplit(".", 1)[0]
        if exc.name not in {module_name, parent_name}:
            raise
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create an import specification for {plugin_file}")
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_service_plugins() -> list[ServicePlugin]:
    """Scan ``toolkit/services/*/plugin.py`` and return all ServicePlugin instances.

    Each plugin's ``service``, ``category``, ``placement``, ``icon`` attributes are
    populated from the ``service.yaml`` next to it — so the plugin knows its
    identity without directory-structure coupling.

    Cached: the scan runs once per process. Call ``_reset_cache()`` in tests
    that add/remove service dirs at runtime.
    """
    global _cache
    if _cache is not None:
        return _cache

    plugins: list[ServicePlugin] = []
    for plugin_dir in sorted(_SERVICES_DIR.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
            continue
        plugin_file = plugin_dir / "plugin.py"
        if not plugin_file.exists():
            continue

        # Dynamic import: toolkit/services/grafana/plugin.py
        module_name = f"toolkit.services.{plugin_dir.name}.plugin"
        try:
            module = _import_plugin_module(module_name, plugin_file)
        except Exception as exc:
            raise RuntimeError(f"failed to import service plugin {plugin_dir.name!r}") from exc

        cls = _find_plugin_class(module)
        if cls is None:
            raise ValueError(f"service plugin {plugin_dir.name!r} does not define a ServicePlugin subclass")

        instance = cls()

        # Populate identity from service.yaml (overrides class-level defaults).
        yaml_data = _load_service_yaml(plugin_dir)
        if yaml_data.get("name"):
            instance.service = yaml_data["name"]
        if yaml_data.get("category"):
            instance.category = yaml_data["category"]
        if yaml_data.get("placement"):
            instance.placement = yaml_data["placement"]
        if yaml_data.get("icon"):
            instance.icon = yaml_data["icon"]
        instance.essential = bool(yaml_data.get("essential", False))
        # Store the manifest and folder for default behavior.
        instance._yaml_data = yaml_data
        instance._plugin_dir = plugin_dir
        _validate_management_contract(instance)
        _validate_generated_artifact_contract(instance)
        _validate_runtime_environment_contract(instance)
        _validate_identity_contract(instance)
        _validate_host_integration_contract(instance)

        plugins.append(instance)

    names = [plugin.service for plugin in plugins]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate service plugin names: {', '.join(duplicates)}")
    _cache = plugins
    return plugins


def get_service_plugin(service: str) -> ServicePlugin | None:
    """Return the plugin for a service name, or None."""
    for plugin in discover_service_plugins():
        if plugin.service == service:
            return plugin
    return None


def all_service_plugins() -> dict[str, ServicePlugin]:
    """Return {service_name: plugin} for all discovered flat-layout services."""
    return {plugin.service: plugin for plugin in discover_service_plugins()}


def load_service_plugins(category: str) -> dict[str, ServicePlugin]:
    """Return {service_name: plugin} for the services in one category.

    Used by deploy_workflow, hook_verify, and watchdog to run category-scoped hooks.
    """
    pending = {plugin.service: plugin for plugin in discover_service_plugins() if plugin.category == category}
    ordered: dict[str, ServicePlugin] = {}
    while pending:
        ready = [
            plugin
            for plugin in pending.values()
            if not set(plugin._yaml_data.get("depends_on", ())).intersection(pending)
        ]
        if not ready:
            raise ValueError(f"service dependency cycle in category {category!r}: {', '.join(sorted(pending))}")
        ready.sort(key=lambda plugin: (int(plugin._yaml_data.get("priority", 0)), plugin.service))
        for plugin in ready:
            ordered[plugin.service] = pending.pop(plugin.service)
    return ordered


def essential_service_plugins() -> list[ServicePlugin]:
    """Return all plugins marked ``essential: true`` in service.yaml."""
    return [plugin for plugin in discover_service_plugins() if plugin.essential]


def heal_routing_map() -> dict[str, ServicePlugin]:
    """Map service/container name → plugin that implements structured heal."""
    routes: dict[str, ServicePlugin] = {}
    for plugin in discover_service_plugins():
        if type(plugin).heal is ServicePlugin.heal:
            continue
        routes[plugin.service] = plugin
        for alias in plugin.heal_aliases:
            routes[alias] = plugin
    return routes


def enabled_service_plugins(cfg, node: str | None = None) -> list[tuple[str, ServicePlugin]]:
    """Return enabled category/plugin pairs, optionally filtered by primary owner.

    ``node`` filters by the manifest's primary placement. Runtime services
    placed on additional nodes are intentionally not treated as duplicate
    plugin owners; use :func:`enabled_plugin_runtimes` when runtime placement is
    the relevant boundary.
    """
    from toolkit.core.compose.registry import enabled_categories

    enabled_cats = enabled_categories(cfg)
    enabled_cat_names = {c.name for c in enabled_cats}
    out: list[tuple[str, ServicePlugin]] = []
    for plugin in discover_service_plugins():
        if plugin.category not in enabled_cat_names:
            continue
        if not plugin.is_enabled(cfg):
            continue
        if node and cfg.is_multi_node and plugin.runtime_node(cfg) != node:
            continue
        out.append((plugin.category, plugin))
    return out


def enabled_plugin_runtimes(cfg, node: str) -> list[tuple[str, ServicePlugin, tuple[str, ...]]]:
    """Return enabled plugin owners and their concrete Compose runtimes on a node.

    The owner service is included only on its primary node. Independently
    placed runtimes are resolved from the manifest so capacity, environment,
    and other runtime-scoped work cannot silently omit secondary nodes.
    """
    from toolkit.core.manifest.placement import manifest_runtime_nodes

    out: list[tuple[str, ServicePlugin, tuple[str, ...]]] = []
    for category, plugin in enabled_service_plugins(cfg):
        runtimes: list[str] = []
        if not cfg.is_multi_node or plugin.runtime_node(cfg) == node:
            runtimes.append(plugin.service)
        runtimes.extend(
            runtime_service
            for runtime_service in plugin.manifest.runtimes
            if node in manifest_runtime_nodes(cfg, plugin.manifest, runtime_service)
        )
        placed = tuple(dict.fromkeys(runtimes))
        if placed:
            out.append((category, plugin, placed))
    return out


def _reset_cache() -> None:
    """Clear the discovery cache. Used by tests that add/remove service dirs."""
    global _cache
    _cache = None
