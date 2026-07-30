"""komodo-core service plugin.

ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml; this file overrides the hooks that
need custom Python logic (verify, oidc_client).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import FleetOnboardingContribution, OIDCClient, ServicePlugin
from toolkit.services.sdk import docker_exec_on_vm, oidc_check_auth_discovery_route

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services import FleetOnboardingContext, RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


class KomodoPlugin(ServicePlugin):
    service = "komodo-core"
    category = "management"

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        if context.services_healthy(services):
            return
        context.warn("komodo-core not healthy; retrying without modifying persisted Mongo data")
        if not context.retry_services(services):
            context.warn("komodo-core retry failed; persisted Mongo data preserved for operator repair")
            return
        if context.wait_until_healthy("komodo-core-retry", ("komodo-core",)):
            context.resolve_failure()
            context.log("komodo-core recovered; cleared its transient wave failure")
        else:
            context.warn("komodo-core still unhealthy; persisted Mongo data preserved for operator repair")

    def reconcile_runtime_credentials(self, cfg: Config, root: Path) -> list[str]:
        import importlib

        bootstrap = importlib.import_module("toolkit.services.komodo-core.bootstrap")
        return bootstrap.reconcile_komodo_runtime_credentials(cfg, root)

    def prepare_fleet_onboarding(
        self,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> FleetOnboardingContribution:
        import importlib

        from toolkit.core.manifest.routes import compile_routes

        route = next(route for route in compile_routes(cfg) if route.service == self.service and route.match is None)
        bootstrap = importlib.import_module("toolkit.services.komodo-core.bootstrap")
        onboarding_key = bootstrap.komodo_onboarding_key(root)
        logs: tuple[str, ...] = ()
        if not onboarding_key:
            logs = ("Komodo: onboarding seed is unavailable - Periphery cannot be paired",)
        variables: dict[str, object] = {
            "komodo_core_address": f"https://{route.host}",
            "komodo_connect_as": host.name,
        }
        if onboarding_key:
            variables["komodo_onboarding_key"] = onboarding_key
        if host.cluster_group:
            variables["fleet_cluster_group"] = host.cluster_group
        return FleetOnboardingContribution(variables=variables, logs=logs)

    def after_fleet_onboarding(self, context: FleetOnboardingContext) -> None:
        import importlib

        if not context.host.cluster_group:
            return

        fleet = importlib.import_module("toolkit.services.komodo-core.fleet")
        for line in fleet.assign_server_cluster_group(context.root, context.host.name, context.host.cluster_group):
            context.log(line)

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "komodo-periphery":
            raise ValueError(f"unsupported Komodo host integration: {integration}")
        from toolkit.services.sdk.host_agents import systemd_unit_active

        active = systemd_unit_active(root, host, "periphery")
        if active is True:
            return True, "periphery active"
        if active is False:
            return False, "periphery inactive"
        return None, "could not query periphery"

    def controller_access_checks(self, cfg: Config, root: Path) -> list[VerifyCheck]:
        from toolkit.core.infra.fleet import all_node_statuses, list_nodes
        from toolkit.services.sdk import VerifyCheck

        nodes = list_nodes(root)
        if not nodes:
            return [VerifyCheck("komodo", "periphery", True, "no fleet nodes registered (skipped)")]
        reconciled = [node for node in nodes if node.reconciled and "komodo-periphery" in node.services]
        if not reconciled:
            return [VerifyCheck("komodo", "periphery", True, "no reconciled Periphery hosts (skipped)")]
        statuses = all_node_statuses(root)
        periphery = [
            status.agent("komodo-periphery") for status in statuses if status.agent("komodo-periphery") is not None
        ]
        active = sum(1 for status in periphery if status.active is True)
        return [
            VerifyCheck(
                "komodo", "periphery", active >= 1, f"{active}/{len(periphery)} fleet node(s) with active periphery"
            )
        ]

    def supported_actions(self) -> frozenset[str]:
        return frozenset({"reconcile-credentials"})

    def execute_action(
        self,
        action: str,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> list[str]:
        if action != "reconcile-credentials":
            raise ValueError("unsupported komodo-core action")
        logs = self.reconcile_runtime_credentials(cfg, root)
        if any(line.startswith("Hook error:") or "incomplete" in line.lower() for line in logs):
            raise RuntimeError("Komodo runtime credential reconciliation did not converge")
        return logs

    @property
    def oidc_client(self) -> OIDCClient:
        return OIDCClient(client_id="komodo", secret_env_var="KOMODO_OIDC_CLIENT_SECRET", native=True)

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """API health, Mongo connectivity, and OIDC config parity."""
        from toolkit.services.sdk import VerifyCheck, authelia_oidc_issuer, container_exists_on_vm, docker_curl

        if not cfg.category_enabled("management"):
            return [VerifyCheck("komodo", "api_health", True, "management not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("komodo", "api_health", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "komodo-core", root):
            return [VerifyCheck("komodo", "api_health", False, "container missing")]

        checks: list[VerifyCheck] = []
        rc, _body = docker_curl(cfg, vm_ip, "komodo-core", "http://localhost:9120/", root=root, timeout=12)
        checks.append(VerifyCheck("komodo", "api_health", rc == 0, "ok" if rc == 0 else f"HTTP {rc}"))

        mongo_rc, mongo_out = docker_exec_on_vm(
            cfg,
            "komodo-core",
            ["bash", "-c", "echo > /dev/tcp/komodo-mongo/27017 && echo MONGO_OK"],
            vm_ip,
            root,
            timeout=15,
        )
        checks.append(
            VerifyCheck(
                "komodo",
                "mongo_connect",
                mongo_rc == 0 and "MONGO_OK" in (mongo_out or ""),
                "komodo-mongo:27017 reachable" if mongo_rc == 0 else (mongo_out or "mongo unreachable")[:120],
            )
        )

        expected = authelia_oidc_issuer(cfg)
        rc_env, out = docker_exec_on_vm(cfg, "komodo-core", ["env"], vm_ip, root)
        if rc_env != 0:
            checks.append(VerifyCheck("komodo", "oidc_issuer", False, "could not read env (container not ready)"))
            return checks
        env = {}
        for line in out.splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                env[key] = val.strip()
        enabled = env.get("KOMODO_OIDC_ENABLED", "").lower() == "true"
        checks.append(
            VerifyCheck(
                "komodo",
                "oidc_enabled",
                enabled,
                "KOMODO_OIDC_ENABLED=true" if enabled else "KOMODO_OIDC_ENABLED not true",
            )
        )
        provider = env.get("KOMODO_OIDC_PROVIDER", "")
        match = provider == expected
        checks.append(
            VerifyCheck(
                "komodo", "oidc_issuer", match, provider if match else f"{provider or '(unset)'} (expected {expected})"
            )
        )
        client_id = env.get("KOMODO_OIDC_CLIENT_ID", "")
        checks.append(
            VerifyCheck(
                "komodo",
                "oidc_client_id",
                client_id == "komodo",
                client_id if client_id == "komodo" else f"{client_id or '(unset)'} (expected komodo)",
            )
        )
        secret_set = bool(env.get("KOMODO_OIDC_CLIENT_SECRET", ""))
        checks.append(
            VerifyCheck(
                "komodo",
                "oidc_client_secret",
                secret_set,
                "client secret set" if secret_set else "KOMODO_OIDC_CLIENT_SECRET empty",
            )
        )
        checks.append(oidc_check_auth_discovery_route(cfg, "komodo", root))
        return checks
