"""Typed adapters from controller operations to existing domain workflows."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolkit.controller.contracts import (
    BackupDrillOperation,
    ConfigApplyOperation,
    ContainerActionOperation,
    DeployOperation,
    DestroyInfraOperation,
    DnsSyncOperation,
    ErrorCode,
    GenerateOperation,
    HostReconcileOperation,
    HostRemoveOperation,
    IdentityOperation,
    JobKind,
    MaintenanceOperation,
    OperationPayload,
    RecoverOperation,
    RestoreDrillOperation,
    SealedInviteUserCommand,
    SecretRotationOperation,
    ServiceActionOperation,
    ServiceVerifyOperation,
    UpdateOperation,
    VerifyOperation,
    WebhookHealOperation,
)
from toolkit.controller.worker import (
    OperationCancelledError,
    OperationContext,
    OperationHandler,
    OperationLeaseLostError,
    OperationRegistry,
    SafeOperationError,
)
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest


class OperationExecutionError(SafeOperationError):
    def __init__(self, message: str, *, code: ErrorCode = "OPERATION_FAILED"):
        super().__init__(code, message)


class OperationPolicyDisabledError(OperationExecutionError):
    def __init__(self, message: str):
        super().__init__(message, code="OPERATION_REJECTED")


def _config_apply_targets(
    cfg: Config,
    owner: ServiceManifest,
    catalog: ServiceCatalog,
) -> tuple[str, ...]:
    """Resolve nodes whose desired runtime can change with an owner's settings."""
    from toolkit.core.config.roles import uses_remote_nodes
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes

    if not uses_remote_nodes(cfg):
        return (cfg.control_node,)

    setting_prefix = f"{owner.name}."
    affected_nodes: set[str] = set()
    for manifest in catalog.manifests:
        predicates = (
            *manifest.enabled_when,
            *(variant.when for route in manifest.routes for variant in route.variants),
        )
        if manifest.category != owner.category and not any(
            predicate.setting is not None and predicate.setting.startswith(setting_prefix) for predicate in predicates
        ):
            continue
        affected_nodes.add(manifest_node(cfg, manifest))
        for runtime_service in manifest.runtimes:
            affected_nodes.update(manifest_runtime_nodes(cfg, manifest, runtime_service))

    ordered = dict.fromkeys((cfg.control_node, *cfg.enabled_nodes))
    return tuple(node for node in ordered if node == cfg.control_node or node in affected_nodes)


def _workflow_log(context: OperationContext, stage: str, *, cancellable: bool = True) -> Callable[[str], None]:
    def log(message: str) -> None:
        if cancellable:
            context.check_cancelled()
        if not message.strip():
            return
        context.log(message, {"stage": stage})

    return log


@contextmanager
def _exclusive_operation(context: OperationContext, root: Path, name: str) -> Iterator[OperationLease]:
    try:
        lease = OperationLease.acquire(root, f"controller-{name}")
    except LeaseBusyError as exc:
        raise OperationExecutionError("Another mutating operation is already running", code="CONFLICT") from exc
    try:
        context.check_cancelled()
        yield lease
    finally:
        lease.release()


def _generate_handler(root: Path) -> OperationHandler:
    def generate(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, GenerateOperation):
            raise OperationExecutionError("invalid generate operation payload")
        from toolkit.core.generate.generate import run_full_generate

        with _exclusive_operation(context, root, "generate"):
            result = run_full_generate(root, validate=operation.validate_output)
        counts = {group: len(paths) for group, paths in result.items()}
        context.log("Generation completed", {"artifact_counts": counts})
        return {"artifact_counts": counts, "validated": operation.validate_output}

    return generate


def _verify_handler(root: Path) -> OperationHandler:
    def verify(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, VerifyOperation):
            raise OperationExecutionError("invalid verify operation payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.verify import verify_all, verify_remote

        with _exclusive_operation(context, root, "verify"):
            cfg = load_config(config_path(root))
            requested_targets = operation.targets or cfg.enabled_nodes
            enabled_targets = [target for target in requested_targets if target in cfg.enabled_nodes]
            if len(enabled_targets) != len(requested_targets):
                raise OperationExecutionError("Verify target is not enabled", code="OPERATION_REJECTED")
            results = {}
            for target in enabled_targets:
                context.check_cancelled()
                current = (
                    verify_remote(root, cfg, vm=target)
                    if cfg.is_multi_node and cfg.proxmox.provision_machines
                    else verify_all(root, cfg, vm=target)
                )
                results.update(current)
        summary = {
            vm: {
                "ok": result.ok,
                "healthy": len(result.services_healthy),
                "unhealthy": len(result.services_unhealthy),
                "pending": len(result.services_pending),
                "url_checks": len(result.url_checks),
            }
            for vm, result in results.items()
        }
        ok = bool(summary) and all(item["ok"] for item in summary.values())
        context.log("Verification completed", {"ok": ok, "nodes": summary})
        if not ok:
            raise OperationExecutionError("verification did not pass")
        return {"ok": True, "nodes": summary}

    return verify


def _service_verify_handler(root: Path) -> OperationHandler:
    def service_verify(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, ServiceVerifyOperation):
            raise OperationExecutionError("invalid service verify operation payload")
        from toolkit.controller.sanitization import sanitize_message
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path, secrets_path
        from toolkit.core.ops.hook_verify import verify_hooks
        from toolkit.core.secrets.secrets import load_secrets_plaintext
        from toolkit.services import get_service_plugin

        cfg = load_config(config_path(root))
        plugin = get_service_plugin(operation.service)
        if plugin is None:
            raise OperationExecutionError("service is not managed", code="OPERATION_REJECTED")
        if not plugin.is_enabled(cfg):
            raise OperationExecutionError("service is disabled", code="OPERATION_REJECTED")
        secret_file = secrets_path(root)
        secrets = load_secrets_plaintext(secret_file) if secret_file.exists() else {}
        context.check_cancelled()
        result = verify_hooks(
            cfg,
            secrets,
            root,
            only_services=frozenset({operation.service}),
            include_framework=False,
            on_progress=lambda message: context.log(message, {"service": operation.service}),
        )
        if not result.checks:
            raise OperationExecutionError("service has no verification checks", code="OPERATION_REJECTED")
        selected_checks = result.checks[:63] if len(result.checks) > 64 else result.checks
        checks = [
            {
                "service": operation.service,
                "check": sanitize_message(check.check)[:63] or "unnamed",
                "status": check.status.value,
                "detail": sanitize_message(check.detail)[:200],
            }
            for check in selected_checks
        ]
        statuses = [check.status.value for check in result.checks]
        if len(result.checks) > 64:
            checks.append(
                {
                    "service": operation.service,
                    "check": "result_limit",
                    "status": "degraded",
                    "detail": f"{len(result.checks) - 63} additional checks were omitted",
                }
            )
            statuses.append("degraded")
        overall = (
            "not_applicable"
            if not statuses or all(item == "not_applicable" for item in statuses)
            else next(
                (item for item in ("fail", "not_ready", "degraded", "pass") if item in statuses),
                "not_applicable",
            )
        )
        return {
            "service": operation.service,
            "checks": checks,
            "overall_status": overall,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    return service_verify


def _deploy_handler(root: Path) -> OperationHandler:
    def deploy(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, DeployOperation):
            raise OperationExecutionError("invalid deploy operation payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

        with _exclusive_operation(context, root, "deploy") as lease:
            cfg = load_config(config_path(root))
            if operation.target is not None and operation.target not in cfg.enabled_nodes:
                raise OperationExecutionError("deploy target is not enabled")
            context.check_cancelled()
            result = asyncio.run(
                run_deploy_workflow(
                    root,
                    cfg,
                    on_log=_workflow_log(context, "deploy"),
                    on_step=lambda step, state: context.log(
                        "Deployment step changed",
                        {"target": operation.target or "all", "step": step, "state": state},
                    ),
                    on_progress=lambda progress: context.log("Deployment progress", progress),
                    targets=(operation.target,) if operation.target else None,
                    skip_infra=operation.skip_infrastructure,
                    skip_dns=operation.skip_dns,
                    operation_lease=lease,
                )
            )
        if not result.success:
            raise OperationExecutionError("deployment did not converge")
        return {"ok": True, "target": operation.target or "all"}

    return deploy


def _recover_handler(root: Path) -> OperationHandler:
    def recover(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, RecoverOperation):
            raise OperationExecutionError("invalid recover operation payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.deploy.deploy_workflow import run_recover_workflow

        with _exclusive_operation(context, root, "recover") as lease:
            cfg = load_config(config_path(root))
            if operation.target is not None and operation.target not in cfg.enabled_nodes:
                raise OperationExecutionError("recover target is not enabled")
            context.check_cancelled()
            result = asyncio.run(
                run_recover_workflow(
                    root,
                    cfg,
                    on_log=_workflow_log(context, "recover"),
                    on_step=lambda step, state: context.log(
                        "Recovery step changed",
                        {"target": operation.target or "all", "step": step, "state": state},
                    ),
                    on_progress=lambda progress: context.log("Recovery progress", progress),
                    vm=operation.target,
                    operation_lease=lease,
                )
            )
        if not result.success:
            raise OperationExecutionError("recovery did not converge")
        return {"ok": True, "target": operation.target or "all"}

    return recover


def _destroy_handler(root: Path) -> OperationHandler:
    def destroy(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, DestroyInfraOperation):
            raise OperationExecutionError("invalid destroy operation payload")
        from toolkit.controller.desired_state_api import machine_retirement_blockers
        from toolkit.core.config.config import Config, load_config, save_config
        from toolkit.core.config.mutations import config_revision, configuration_lock
        from toolkit.core.config.storage import config_path

        if operation.action == "destroy_all":
            if context.actor != "local:operator":
                raise OperationPolicyDisabledError("infrastructure destruction requires a local operator")
            from toolkit.core.infra.infra_destroy import destroy_infrastructure_guarded

            with _exclusive_operation(context, root, "destroy") as lease:
                with configuration_lock(root):
                    if config_revision(root) != operation.config_revision:
                        raise OperationPolicyDisabledError("configuration changed after destructive approval")
                    cfg = load_config(config_path(root))
                    if set(operation.scopes) != set(cfg.enabled_nodes):
                        raise OperationPolicyDisabledError("full destruction must include every enabled machine")
                context.check_cancelled()
                code = destroy_infrastructure_guarded(
                    root,
                    on_log=_workflow_log(context, "destroy"),
                    scope=operation.scopes,
                    operation_lease=lease,
                )
            if code != 0:
                raise OperationExecutionError("infrastructure destruction failed verification")
            return {"ok": True, "action": operation.action, "scopes": operation.scopes}

        if len(operation.scopes) != 1:
            raise OperationPolicyDisabledError("retirement must target exactly one machine")
        machine_id = operation.scopes[0]
        from toolkit.core.infra.infra_destroy import retire_machine_infrastructure_guarded

        with _exclusive_operation(context, root, "retire") as lease:
            with configuration_lock(root):
                if config_revision(root) != operation.config_revision:
                    raise OperationPolicyDisabledError("configuration changed after retirement approval")
                cfg = load_config(config_path(root))
                blockers = machine_retirement_blockers(root, cfg, machine_id)
                if blockers:
                    raise OperationPolicyDisabledError("machine retirement is blocked: " + "; ".join(blockers))
            context.check_cancelled()
            code = retire_machine_infrastructure_guarded(
                root,
                machine_id,
                on_log=_workflow_log(context, "retire"),
                operation_lease=lease,
            )
            if code != 0:
                raise OperationExecutionError("machine retirement failed verification")
            with configuration_lock(root):
                if config_revision(root) != operation.config_revision:
                    raise OperationExecutionError("configuration changed during retirement", code="CONFLICT")
                updated = cfg.model_copy(deep=True)
                updated.machines.pop(machine_id)
                save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
            from toolkit.core.generate.generate import run_full_generate

            run_full_generate(root, validate=True)
        return {"ok": True, "action": operation.action, "scopes": operation.scopes}

    return destroy


def _dns_handler(root: Path) -> OperationHandler:
    def dns(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, DnsSyncOperation):
            raise OperationExecutionError("invalid DNS operation payload")
        from toolkit.core.ops.dns import cleanup_stale_homelab_dns, sync_cloudflare_dns

        with _exclusive_operation(context, root, "dns"):
            if operation.action == "cleanup":
                if operation.dry_run:
                    raise OperationExecutionError("DNS cleanup does not support dry-run", code="OPERATION_REJECTED")
                deleted = cleanup_stale_homelab_dns(root)
                stats = {"deleted": deleted}
            else:
                stats = sync_cloudflare_dns(root, dry_run=operation.dry_run, on_log=_workflow_log(context, "dns"))
        return {"ok": True, "stats": stats}

    return dns


def _maintenance_handler(root: Path) -> OperationHandler:
    def maintenance(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, MaintenanceOperation):
            raise OperationExecutionError("invalid maintenance operation payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.cluster_maintenance import run_cluster_maintenance

        with _exclusive_operation(context, root, "maintenance"):
            cfg = load_config(config_path(root))
            result = run_cluster_maintenance(
                cfg,
                root,
                actor=context.actor,
                on_log=context.log,
                check_cancelled=context.check_cancelled,
            )
        context.log(
            "Maintenance completed",
            {
                "ok": result.ok,
                "node_count": len(result.nodes),
                "action_count": len(result.actions),
                "error_count": len(result.errors),
            },
        )
        if not result.ok:
            raise OperationExecutionError("maintenance completed with errors")
        return {"ok": True, "node_count": len(result.nodes), "action_count": len(result.actions)}

    return maintenance


def _backup_drill_handler(root: Path) -> OperationHandler:
    def backup_drill(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, BackupDrillOperation):
            raise OperationExecutionError("invalid backup drill payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.backup_restore_drill import run_backup_restore_drill

        context.check_cancelled()
        cfg = load_config(config_path(root))
        result = run_backup_restore_drill(cfg, root, actor=context.actor)
        artifact_count = sum(node.artifact_count for node in result.nodes)
        context.log(
            "Backup content drill completed",
            {
                "ok": result.ok,
                "node_count": len(result.nodes),
                "artifact_count": artifact_count,
                "error_count": len(result.errors),
            },
        )
        if result.deferred:
            raise OperationExecutionError("another mutating operation is already running", code="CONFLICT")
        if not result.ok:
            raise OperationExecutionError("backup content drill failed")
        return {"ok": True, "node_count": len(result.nodes), "artifact_count": artifact_count}

    return backup_drill


def _restore_drill_handler(root: Path) -> OperationHandler:
    def restore_drill(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, RestoreDrillOperation):
            raise OperationExecutionError("invalid restore drill payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.db_safety import list_dumps
        from toolkit.core.ops.restore_drill import run_restore_drill

        cfg = load_config(config_path(root))
        record = next((item for item in list_dumps(cfg, root) if item.dump_id == operation.dump_id), None)
        if record is None:
            raise OperationExecutionError("restore drill dump is unavailable")
        context.check_cancelled()
        result = run_restore_drill(cfg, root, record, actor=context.actor)
        if not result.ok:
            raise OperationExecutionError("restore drill failed")
        return {
            "ok": True,
            "database_count": result.database_count,
            "checkpoint_id": result.checkpoint_id,
        }

    return restore_drill


def _config_apply_handler(root: Path) -> OperationHandler:
    def config_apply(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, ConfigApplyOperation):
            raise OperationExecutionError("invalid configuration apply payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.mutations import config_revision
        from toolkit.core.config.storage import config_path
        from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
        from toolkit.services import get_service_plugin

        with _exclusive_operation(context, root, "config-apply") as lease:
            if config_revision(root) != operation.revision_hash:
                raise OperationExecutionError("configuration revision was superseded", code="CONFLICT")
            plugin = get_service_plugin(operation.service)
            if plugin is None:
                raise OperationExecutionError("service is not managed", code="OPERATION_REJECTED")
            cfg = load_config(config_path(root))
            service_owner = plugin.runtime_node(cfg)
            from toolkit.core.manifest.catalog import load_service_catalog

            targets = _config_apply_targets(cfg, plugin.manifest, load_service_catalog())
            preserve_controller = (
                plugin.has_compose_application
                and "homelab-controller" in plugin.compose_application().get("services", {})
            )
            context.check_cancelled()
            context.log("Applying service configuration", {"service": operation.service, "node": service_owner})
            result = asyncio.run(
                run_deploy_workflow(
                    root,
                    cfg,
                    on_log=_workflow_log(context, "config-apply"),
                    on_step=lambda step, state: context.log(
                        "Configuration apply step changed",
                        {"service": operation.service, "step": step, "state": state},
                    ),
                    on_progress=lambda progress: context.log("Configuration apply progress", progress),
                    skip_infra=True,
                    skip_dns=False,
                    targets=targets,
                    preserve_controller=preserve_controller,
                    operation_lease=lease,
                )
            )
        if not result.success:
            raise OperationExecutionError("service configuration reconciliation failed")
        context.log("Service configuration applied", {"service": operation.service, "node": service_owner})
        return {"ok": True, "service": operation.service, "nodes": list(targets)}

    return config_apply


def _host_reconcile_handler(root: Path) -> OperationHandler:
    def reconcile(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, HostReconcileOperation):
            raise OperationExecutionError("invalid managed host reconciliation payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path

        log = _workflow_log(context, "host")
        with _exclusive_operation(context, root, "host") as lease:
            cfg = load_config(config_path(root))
            host = next((item for item in cfg.external_hosts if item.name == operation.host_name), None)
            if host is None:
                raise OperationExecutionError("managed host is unavailable", code="NOT_FOUND")
            if host.kind == "fleet":
                from toolkit.core.infra.fleet import onboard_node

                fleet_result = onboard_node(root, host.name, on_log=log, operation_lease=lease)
                if not fleet_result.success:
                    raise OperationExecutionError("managed fleet host did not reconcile")
                strategy = "fleet"
            else:
                from toolkit.core.deploy.external_deploy import deploy_external_host
                from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG
                from toolkit.core.infra.hosts import (
                    managed_host_fingerprint,
                    mark_host_reconciled,
                    reconcile_host_integrations,
                    trust_host_key,
                )

                expected_fingerprint = managed_host_fingerprint(host)

                for message in trust_host_key(host, root=root):
                    log(message)
                integration_result = reconcile_host_integrations(root, host)
                for message in integration_result.logs:
                    log(message)
                if not integration_result.ok:
                    raise OperationExecutionError("managed host integrations did not reconcile")
                remote_agents = {
                    service.name
                    for service in FLEET_SERVICE_CATALOG
                    if service.is_selectable_for("plain") and service.ansible_role
                }
                if any(service in remote_agents for service in host.services):
                    external_result = deploy_external_host(root, cfg, host, on_log=log)
                    if not external_result.success:
                        raise OperationExecutionError("managed external host did not reconcile")
                else:
                    log("No remote agent roles selected; controller integrations are reconciled")
                if integration_result.refresh_nodes:
                    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

                    refresh_nodes = integration_result.refresh_nodes
                    context.log(
                        "Refreshing runtime nodes for managed-host desired state",
                        {"stage": "host", "nodes": list(refresh_nodes)},
                    )
                    refresh_result = asyncio.run(
                        run_deploy_workflow(
                            root,
                            load_config(config_path(root)),
                            on_log=_workflow_log(context, "host-runtime-refresh"),
                            on_step=lambda step, state: context.log(
                                "Managed-host runtime refresh step changed",
                                {"step": step, "state": state},
                            ),
                            on_progress=lambda progress: context.log("Managed-host runtime refresh progress", progress),
                            skip_infra=True,
                            skip_dns=True,
                            targets=refresh_nodes,
                            operation_lease=lease,
                        )
                    )
                    if not refresh_result.success:
                        raise OperationExecutionError("managed-host runtime refresh failed")
                if not mark_host_reconciled(root, host.name, expected_fingerprint):
                    raise OperationExecutionError("managed host changed during reconciliation", code="CONFLICT")
                strategy = "external"
        return {"ok": True, "host_name": host.name, "strategy": strategy}

    return reconcile


def _host_remove_handler(root: Path) -> OperationHandler:
    def remove(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, HostRemoveOperation):
            raise OperationExecutionError("invalid managed host removal payload")
        from toolkit.controller.desired_state_api import DesiredStateConflictError
        from toolkit.controller.managed_hosts_api import remove_managed_host

        try:
            with _exclusive_operation(context, root, "host") as lease:
                remove_managed_host(
                    root,
                    operation.host_name,
                    operation.expected_fingerprint,
                    on_log=_workflow_log(context, "host"),
                    operation_lease=lease,
                )
        except DesiredStateConflictError as exc:
            raise OperationExecutionError("managed host changed; reload and retry", code="CONFLICT") from exc
        return {"ok": True, "host_name": operation.host_name}

    return remove


def _container_action_handler(root: Path) -> OperationHandler:
    def container_action(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, ContainerActionOperation):
            raise OperationExecutionError("invalid container action payload")
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
        from toolkit.core.config.config import load_config
        from toolkit.core.config.service_metadata import _load_all_services
        from toolkit.core.config.storage import config_path

        cfg = load_config(config_path(root))
        metadata = _load_all_services()
        service = metadata.get(operation.service)
        container_name = operation.service
        if service:
            node = str(service["node"])
        elif operation.service.startswith("project-"):
            subdomain = operation.service.removeprefix("project-")
            project = next((entry for entry in cfg.projects.entries if entry.subdomain == subdomain), None)
            if project is None:
                raise OperationExecutionError("service is not managed", code="OPERATION_REJECTED")
            from toolkit.core.projects.placement import project_node

            node = project_node(cfg, project)
            container_name = project.subdomain
        else:
            raise OperationExecutionError("service is not managed", code="OPERATION_REJECTED")
        if node not in cfg.enabled_nodes:
            raise OperationExecutionError("service node is not enabled", code="OPERATION_REJECTED")
        context.check_cancelled()
        if cfg.is_multi_node:
            code, _stdout, _stderr = ssh_run_on_vm(
                cfg,
                cfg.node_ip(node),
                f"docker {operation.action} {shlex.quote(container_name)}",
                root=root,
                timeout=60,
                retries=1,
            )
        else:
            result = subprocess.run(
                ["docker", operation.action, container_name],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            code = result.returncode
        if code != 0:
            raise OperationExecutionError("container action failed")
        context.log(
            "Container action completed",
            {"service": operation.service, "action": operation.action, "node": node},
        )
        return {"ok": True, "service": operation.service, "action": operation.action, "node": node}

    return container_action


def _service_action_handler(root: Path) -> OperationHandler:
    def service_action(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, ServiceActionOperation):
            raise OperationExecutionError("invalid service action payload")
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path, secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext
        from toolkit.services import get_service_plugin

        plugin = get_service_plugin(operation.service)
        if plugin is None:
            raise OperationExecutionError("service is not managed", code="OPERATION_REJECTED")
        declared = {action.id for action in plugin.management().actions}
        if operation.action not in declared or operation.action not in plugin.supported_actions():
            raise OperationExecutionError("service action is not supported", code="OPERATION_REJECTED")
        cfg = load_config(config_path(root))
        if not plugin.is_enabled(cfg):
            raise OperationExecutionError("service is disabled", code="OPERATION_REJECTED")
        secret_file = secrets_path(root)
        secrets = load_secrets_plaintext(secret_file) if secret_file.exists() else {}
        try:
            with _exclusive_operation(context, root, "service-action"):
                lines = plugin.execute_action(operation.action, cfg, secrets, root)
                if not isinstance(lines, list) or len(lines) > 100:
                    raise OperationExecutionError("service action returned invalid progress")
                for line in lines:
                    if not isinstance(line, str) or not line.strip():
                        raise OperationExecutionError("service action returned invalid progress")
                    context.log(
                        line[:500],
                        {"service": operation.service, "action": operation.action},
                    )
        except (OperationCancelledError, OperationLeaseLostError, SafeOperationError):
            raise
        except Exception as exc:
            raise OperationExecutionError("service action failed") from exc
        return {"ok": True, "service": operation.service, "action": operation.action}

    return service_action


def _identity_handler(root: Path) -> OperationHandler:
    def identity(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, IdentityOperation):
            raise OperationExecutionError("invalid identity operation payload")
        from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

        persisted_command = operation.command
        audit_action = (
            "IDENTITY_INVITE"
            if isinstance(persisted_command, SealedInviteUserCommand)
            else f"IDENTITY_{persisted_command.action.upper()}"
        )
        requested_user_id = getattr(persisted_command, "user_id", "new")
        try:
            command = persisted_command
            if isinstance(command, SealedInviteUserCommand):
                persisted_job = context.store.get_job(context.job_id)
                command = context.store.open_invite_command(
                    command,
                    principal=context.actor,
                    idempotency_key=persisted_job.request.idempotency_key,
                )
            with _exclusive_operation(context, root, "identity"):
                result = execute_directory_command(
                    root,
                    command,
                    on_progress=context.log,
                    check_cancelled=context.check_cancelled,
                    replay=context.lease_generation > 1,
                    execution_id=context.job_id,
                )
            user_id = str(result.get("user_id") or requested_user_id)
            result_outcome = str(result.get("outcome") or "completed")
            context.store.append_audit(
                context.actor,
                audit_action,
                f"identity:{user_id}",
                "FAILED" if result_outcome == "partial_failure" else "ALLOWED",
                {"job_id": context.job_id, "outcome": result_outcome},
            )
            return result
        except DirectoryMutationError as exc:
            context.store.append_audit(
                context.actor,
                audit_action,
                f"identity:{requested_user_id}",
                "FAILED",
                {"job_id": context.job_id, "code": exc.code},
            )
            raise OperationExecutionError(exc.safe_message, code=exc.code) from exc
        except (OperationCancelledError, OperationLeaseLostError):
            raise
        except SafeOperationError as exc:
            context.store.append_audit(
                context.actor,
                audit_action,
                f"identity:{requested_user_id}",
                "FAILED",
                {"job_id": context.job_id, "code": exc.code},
            )
            raise
        except Exception as exc:
            context.store.append_audit(
                context.actor,
                audit_action,
                f"identity:{requested_user_id}",
                "FAILED",
                {"job_id": context.job_id, "code": "OPERATION_FAILED"},
            )
            raise OperationExecutionError("Identity operation failed") from exc

    return identity


def _webhook_heal_handler(root: Path) -> OperationHandler:
    def webhook_heal(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, WebhookHealOperation):
            raise OperationExecutionError("invalid webhook heal operation payload")
        if context.actor != "webhook:grafana":
            raise OperationPolicyDisabledError("webhook heal requires the Grafana integration principal")
        from toolkit.controller.inventory_api import read_services_view
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.watchdog import Watchdog, WatchdogReport

        with _exclusive_operation(context, root, "webhook-heal"):
            managed = {
                service.name
                for category in read_services_view(root, family=False, groups=[]).categories
                for service in category.services
            }
            if operation.service not in managed:
                raise OperationPolicyDisabledError("webhook heal target is not an enabled managed service")
            watchdog = Watchdog(root, load_config(config_path(root)))
            if operation.service not in watchdog.restartable_services():
                raise OperationPolicyDisabledError("webhook heal target is not safe for unattended restart")
            observed = watchdog.check_all()
            issues = [issue for issue in observed.issues if issue.service == operation.service and issue.auto_fixable]
            if not issues:
                context.log("Webhook heal skipped", {"service": operation.service, "reason": "currently_healthy"})
                return {
                    "ok": True,
                    "service": operation.service,
                    "action": "skipped",
                    "reason": "currently_healthy",
                }
            targeted = WatchdogReport(issues=issues)
            result = watchdog.heal_targeted(targeted, service=operation.service)
        if result.failed:
            context.log(
                "Webhook heal failed",
                {"service": operation.service, "attempted": result.attempted, "failed": result.failed},
            )
            raise OperationExecutionError("Webhook heal remedy failed")
        context.log(
            "Webhook heal completed",
            {
                "service": operation.service,
                "attempted": result.attempted,
                "succeeded": result.succeeded,
                "deferred": result.deferred,
            },
        )
        return {
            "ok": True,
            "service": operation.service,
            "action": "succeeded" if result.succeeded else "deferred",
        }

    return webhook_heal


def _update_handler(root: Path) -> OperationHandler:
    def update(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, UpdateOperation):
            raise OperationExecutionError("invalid update operation payload")
        from datetime import UTC, datetime

        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.ops.release_state import (
            clear_recovery_release,
            clear_rollback_release,
            load_active_release,
            load_recovery_release,
            load_rollback_release,
            write_active_release,
            write_recovery_release,
            write_rollback_release,
        )
        from toolkit.core.ops.release_update import (
            ReleaseUpdateError,
            affected_roles,
            build_updated_release,
            resolve_target_digest,
            selected_services_require_backup,
            snapshot_update_roles,
        )
        from toolkit.core.ops.update_plan import UpdatePlanError, load_current_update_plan

        if operation.action == "refresh":
            from toolkit.core.ops.update_plan import write_update_scan_compose
            from toolkit.core.ops.updates import UpdateCheckError, run_check

            context.log("Refreshing compatible image updates", {"stage": "discovery"})
            cfg = load_config(config_path(root))
            try:
                report = run_check(root, refresh=True, compose_file=write_update_scan_compose(root, cfg))
            except UpdateCheckError as exc:
                raise OperationExecutionError(str(exc)) from exc
            if not report:
                raise OperationExecutionError("update discovery failed; the previous plan was preserved")
            plan = load_current_update_plan(root, cfg)
            count = len(plan.candidates) if plan is not None else 0
            context.log("Update discovery completed", {"stage": "discovery", "candidates": count})
            return {"ok": True, "action": "refresh", "candidates": count}

        cfg = load_config(config_path(root))
        active = load_active_release(root)
        recovery = load_recovery_release(root)

        async def deploy(roles: tuple[str, ...], stage: str) -> bool:
            from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

            result = await run_deploy_workflow(
                root,
                cfg,
                on_log=_workflow_log(context, stage),
                on_step=lambda step, state: context.log(
                    "Update deployment step changed",
                    {"stage": stage, "step": step, "state": state},
                ),
                on_progress=lambda progress: context.log("Update deployment progress", progress),
                targets=roles,
                skip_infra=True,
                skip_dns=True,
            )
            return result.success

        if operation.action == "recover":
            if recovery is None:
                raise OperationExecutionError(
                    "no incomplete automatic rollback requires recovery",
                    code="OPERATION_REJECTED",
                )
            if active != recovery.previous:
                raise OperationExecutionError("active release was superseded", code="CONFLICT")
            changed = set(recovery.failed.images) | (set(active.images) if active is not None else set())
            roles = affected_roles(root, cfg, changed)
            context.log("Recovering the previous verified release", {"stage": "rollback", "nodes": list(roles)})
            if not asyncio.run(deploy(roles, "rollback")):
                raise OperationExecutionError("release recovery did not converge; recovery state was preserved")
            clear_recovery_release(root)
            clear_rollback_release(root)
            return {"ok": True, "action": "recover", "nodes": list(roles)}

        if recovery is not None:
            raise OperationExecutionError(
                "automatic rollback recovery is required before another update operation",
                code="OPERATION_REJECTED",
            )

        if operation.action == "rollback":
            rollback = load_rollback_release(root)
            if active is None or active.revision != operation.revision:
                raise OperationExecutionError("active release was superseded", code="CONFLICT")
            if rollback is None or rollback.expected_active_revision != active.revision:
                raise OperationExecutionError("no matching rollback release is available", code="OPERATION_REJECTED")
            changed = set(active.images) | (set(rollback.previous.images) if rollback.previous else set())
            roles = affected_roles(root, cfg, changed)
            write_active_release(root, rollback.previous)
            context.log("Restoring previous release", {"stage": "rollback", "nodes": list(roles)})
            if not asyncio.run(deploy(roles, "rollback")):
                write_active_release(root, active)
                raise OperationExecutionError("rollback did not converge; active release marker was restored")
            clear_rollback_release(root)
            return {"ok": True, "action": "rollback", "nodes": list(roles)}

        try:
            plan = load_current_update_plan(root, cfg)
            if plan is None or plan.revision != operation.revision:
                raise OperationExecutionError("update plan was superseded", code="CONFLICT")
            selected = set(operation.services)
            candidates = {candidate.service: candidate for candidate in plan.candidates}
            if not selected <= candidates.keys():
                raise OperationExecutionError("selected update is not in the active plan", code="OPERATION_REJECTED")
            roles = affected_roles(root, cfg, selected)
            resolved: dict[str, str] = {}
            for service in sorted(selected):
                context.check_cancelled()
                context.log("Resolving immutable image digest", {"stage": "resolve", "service": service})
                resolved[service] = resolve_target_digest(candidates[service])
            if selected_services_require_backup(root, cfg, selected):
                if not cfg.backups.enabled:
                    raise OperationExecutionError(
                        "stateful service updates require configured encrypted backups",
                        code="OPERATION_REJECTED",
                    )
                context.log("Creating pre-update snapshots", {"stage": "backup", "nodes": list(roles)})
                snapshot_update_roles(
                    root,
                    cfg,
                    roles,
                    actor=context.actor,
                    on_result=lambda role, ok: context.log(
                        "Pre-update snapshot completed" if ok else "Pre-update snapshot failed",
                        {"stage": "backup", "node": role, "ok": ok},
                    ),
                )
                current_plan = load_current_update_plan(root, cfg)
                if current_plan is None or current_plan.revision != operation.revision:
                    raise OperationExecutionError("update plan changed during backup", code="CONFLICT")
            release = build_updated_release(
                active,
                resolved,
                {service: candidates[service].target_image for service in selected},
                created_at=datetime.now(UTC).isoformat(),
            )
        except (ReleaseUpdateError, UpdatePlanError) as exc:
            raise OperationExecutionError(str(exc), code="OPERATION_REJECTED") from exc

        write_rollback_release(root, expected_active_revision=release.revision, previous=active)
        write_active_release(root, release)
        context.log(
            "Activating immutable release",
            {"stage": "deploy", "revision": release.revision, "nodes": list(roles)},
        )
        if asyncio.run(deploy(roles, "update")):
            return {
                "ok": True,
                "action": "apply",
                "revision": release.revision,
                "services": sorted(selected),
                "nodes": list(roles),
            }

        write_active_release(root, active)
        context.log("Update verification failed; restoring previous release", {"stage": "rollback"})
        write_recovery_release(root, previous=active, failed=release)
        rollback_ok = asyncio.run(deploy(roles, "rollback"))
        if rollback_ok:
            clear_recovery_release(root)
            clear_rollback_release(root)
            raise OperationExecutionError("update verification failed; previous release was restored")
        context.log("Automatic rollback did not converge; explicit recovery is required", {"stage": "rollback"})
        raise OperationExecutionError("update and automatic rollback both failed; explicit recovery is required")

    return update


def _disabled_handler(expected_type: type, reason: str) -> OperationHandler:
    def disabled(_context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, expected_type):
            raise OperationExecutionError("operation payload type does not match its handler")
        raise OperationPolicyDisabledError(reason)

    return disabled


def _secret_rotation_handler(root: Path) -> OperationHandler:
    def rotate(context: OperationContext, operation: OperationPayload) -> dict[str, Any]:
        if not isinstance(operation, SecretRotationOperation):
            raise OperationExecutionError("invalid secret rotation operation")
        from toolkit.controller.settings_api import (
            restore_secret_values,
            rotate_secret_values,
        )
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
        from toolkit.core.ops.db_safety import pre_deploy_dump
        from toolkit.core.secrets.rotation_context import previous_secret_context

        with _exclusive_operation(context, root, "secret-rotation") as lease:
            cfg = load_config(config_path(root))
            context.log("Creating safety database snapshot", {"stage": "preflight"})
            pre_deploy_dump(cfg, root)
            context.check_cancelled()
            before, rotated = rotate_secret_values(root, operation.secret_names)
            try:
                context.log("Rotated generated credentials", {"stage": "secrets", "count": len(rotated)})
                with previous_secret_context(root, before):
                    result = asyncio.run(
                        run_deploy_workflow(
                            root,
                            cfg,
                            on_log=_workflow_log(context, "deploy"),
                            on_step=lambda step, state: context.log(
                                "Deployment step changed", {"step": step, "state": state}
                            ),
                            on_progress=lambda progress: context.log("Deployment progress", progress),
                            operation_lease=lease,
                        )
                    )
                if not result.success:
                    raise OperationExecutionError("deployment after secret rotation did not converge")
            except BaseException as exc:
                lease_lost = isinstance(exc, OperationLeaseLostError)

                def recovery_event(message: str, payload: dict[str, Any]) -> None:
                    nonlocal lease_lost
                    if lease_lost:
                        return
                    try:
                        context.log(message, payload)
                    except OperationLeaseLostError:
                        lease_lost = True

                with lease.shield_cancellation():
                    recovery_event("Rotation failed; restoring previous credentials", {"stage": "rollback"})
                    try:
                        restore_secret_values(root, before, rotated)
                        with previous_secret_context(root, rotated):
                            rollback = asyncio.run(
                                run_deploy_workflow(
                                    root,
                                    cfg,
                                    on_log=lambda message: recovery_event(message, {"stage": "rollback"}),
                                    on_step=lambda step, state: recovery_event(
                                        "Rollback step changed", {"step": step, "state": state}
                                    ),
                                    on_progress=lambda progress: recovery_event("Rollback progress", progress),
                                    operation_lease=lease,
                                )
                            )
                        if not rollback.success:
                            raise OperationExecutionError(
                                "secret rotation failed and rollback did not converge"
                            ) from exc
                    except OperationExecutionError:
                        raise
                    except OperationLeaseLostError:
                        raise
                    except BaseException as rollback_exc:
                        raise OperationExecutionError(
                            "secret rotation failed and rollback was blocked"
                        ) from rollback_exc
                if lease_lost:
                    raise OperationLeaseLostError("operation lease was lost after credential rollback") from exc
                context.check_cancelled()
                raise OperationExecutionError("secret rotation failed; previous credentials restored") from exc
        return {"ok": True, "changed_names": sorted(rotated)}

    return rotate


def build_operation_registry(root: Path) -> OperationRegistry:
    root = root.resolve()
    registry = OperationRegistry()
    registry.register(JobKind.GENERATE, _generate_handler(root))
    registry.register(JobKind.VERIFY, _verify_handler(root))
    registry.register(JobKind.SERVICE_VERIFY, _service_verify_handler(root))
    registry.register(JobKind.DEPLOY, _deploy_handler(root))
    registry.register(JobKind.RECOVER, _recover_handler(root))
    registry.register(JobKind.DESTROY_INFRA, _destroy_handler(root))
    registry.register(JobKind.DNS_SYNC, _dns_handler(root))
    registry.register(JobKind.MAINTENANCE, _maintenance_handler(root))
    registry.register(JobKind.BACKUP_DRILL, _backup_drill_handler(root))
    registry.register(JobKind.RESTORE_DRILL, _restore_drill_handler(root))
    registry.register(JobKind.HOST_RECONCILE, _host_reconcile_handler(root))
    registry.register(JobKind.HOST_REMOVE, _host_remove_handler(root))
    registry.register(JobKind.CONTAINER_ACTION, _container_action_handler(root))
    registry.register(JobKind.SERVICE_ACTION, _service_action_handler(root))
    registry.register(JobKind.IDENTITY, _identity_handler(root))
    registry.register(JobKind.WEBHOOK_HEAL, _webhook_heal_handler(root))
    registry.register(JobKind.CONFIG_APPLY, _config_apply_handler(root))
    registry.register(JobKind.UPDATE, _update_handler(root))
    registry.register(JobKind.SECRET_ROTATION, _secret_rotation_handler(root))
    return registry
