"""Revisioned controller-owned desired-state reads and narrow mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from toolkit.controller.read_models import (
    DnsIpUpdate,
    DnsRecordView,
    DnsView,
    MachineCreate,
    MachineRemove,
    MachinesView,
    MachineTemplateView,
    MachineUpdate,
    MachineView,
    ProjectCreate,
    ProjectDatabaseOption,
    ProjectPlacementOption,
    ProjectRemove,
    ProjectsView,
    ProjectView,
    SettingsUpdate,
    SettingsValues,
    SettingsView,
)
from toolkit.core.config.config import (
    DEFAULT_SMTP_PASSWORD_SECRET,
    Config,
    ProjectEntry,
    ServicesConfig,
    load_config,
    save_config,
    save_local_config,
)
from toolkit.core.config.mutations import config_revision, configuration_lock, configuration_mutation
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.machines import MachineSpec
from toolkit.core.ops.dns import desired_records_from_config, resolve_public_dns_ip
from toolkit.core.ops.notifications import probe_smtp_transport, resolve_smtp_transport
from toolkit.core.projects.placement import project_placement_options
from toolkit.core.secrets.secrets import (
    SecretTier,
    get_required_secrets,
    load_secrets_plaintext,
    save_secrets_plaintext,
)


class DesiredStateConflictError(RuntimeError):
    pass


class DesiredStateValidationError(RuntimeError):
    pass


class SMTPSettingsValidationError(DesiredStateValidationError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _dns_source(value: str) -> Literal["config", "override", "autodetect", "proxmox-url", "missing"]:
    if value == "config":
        return "config"
    if value == "override":
        return "override"
    if value == "autodetect":
        return "autodetect"
    if value == "proxmox-url":
        return "proxmox-url"
    return "missing"


def read_dns_view(root: Path) -> DnsView:
    root = root.resolve()
    with configuration_lock(root):
        cfg = load_config(config_path(root))
        public_ip, source = resolve_public_dns_ip(cfg)
        records = desired_records_from_config(cfg, public_ip or "<your-public-ip>")
        secret_path = secrets_path(root)
        secrets = load_secrets_plaintext(secret_path) if secret_path.exists() else {}
        return DnsView(
            revision=config_revision(root),
            public_ip=public_ip or "",
            ip_source=_dns_source(source),
            records=[
                DnsRecordView(
                    type=record.type,
                    name=record.name,
                    content=record.content,
                    is_proxied=record.proxied,
                )
                for record in records
                if not record.disabled
            ],
            has_cloudflare_credentials=bool(secrets.get("CLOUDFLARE_API_TOKEN") and secrets.get("CLOUDFLARE_ZONE_ID")),
        )


def update_dns_public_ip(root: Path, update: DnsIpUpdate) -> DnsView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:dns"):
        if config_revision(root) != update.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        candidate, _source = resolve_public_dns_ip(cfg, update.public_ip.strip())
        if not candidate:
            raise DesiredStateValidationError("public IP is invalid")
        updated = cfg.model_copy(update={"dns": cfg.dns.model_copy(update={"public_ip": candidate})})
        save_config(updated, config_path(root))
    return read_dns_view(root)


def _settings_values(cfg: Config, secrets: dict[str, str] | None = None) -> SettingsValues:
    toggles = _service_toggles()
    secrets = secrets or {}
    smtp_secret = cfg.notifications.smtp.password_secret
    return SettingsValues(
        domain=cfg.domain,
        email=cfg.email,
        timezone=cfg.timezone,
        services={name: cfg.category_enabled(name) for name in toggles},
        deploy_ntfy_url=cfg.notifications.deploy_ntfy_url,
        smtp_mode=cfg.notifications.smtp.mode,
        smtp_host=cfg.notifications.smtp.host,
        smtp_port=cfg.notifications.smtp.port,
        smtp_starttls=cfg.notifications.smtp.starttls,
        smtp_username=cfg.notifications.smtp.username,
        smtp_password_secret=cfg.notifications.smtp.password_secret,
        smtp_password_configured=bool(smtp_secret and secrets.get(smtp_secret)),
        smtp_from_address=cfg.notifications.smtp.from_address,
        ssh_auth=cfg.ssh.auth_method,
        ssh_key_file=cfg.ssh.key_file,
        proxmox_api_url=cfg.proxmox.api_url,
        proxmox_control_host=cfg.proxmox.control_host,
        proxmox_ssh_user=cfg.proxmox.ssh.user,
        proxmox_ssh_port=cfg.proxmox.ssh.port,
        proxmox_ssh_key_file=cfg.proxmox.ssh.key_file,
        proxmox_ssh_connect_timeout=cfg.proxmox.ssh.connect_timeout,
        proxmox_ssh_command_timeout=cfg.proxmox.ssh.command_timeout,
        proxmox_ssh_retries=cfg.proxmox.ssh.retries,
        proxmox_node=cfg.proxmox.node,
        proxmox_storage=cfg.proxmox.lxc_storage,
        proxmox_template_datastore=cfg.proxmox.lxc_template_datastore,
        proxmox_template_url=cfg.proxmox.lxc_template_url,
        proxmox_template_checksum=cfg.proxmox.lxc_template_checksum,
        proxmox_tls_ca_file=cfg.proxmox.tls_ca_file,
        proxmox_provision_machines=cfg.proxmox.provision_machines,
        expose_internet=cfg.network.expose_via_internet,
        container_ipv4_cidr=cfg.network.container_ipv4_cidr,
        container_network_prefix=cfg.network.container_network_prefix,
        dns_provider=cfg.dns.provider,
        dns_public_ip=cfg.dns.public_ip,
        dns_proxy=cfg.dns.proxy_enabled,
    )


def read_settings_view(root: Path) -> SettingsView:
    root = root.resolve()
    with configuration_lock(root):
        cfg = load_config(config_path(root))
        secret_path = secrets_path(root)
        secrets = load_secrets_plaintext(secret_path) if secret_path.exists() else {}
        return SettingsView(
            revision=config_revision(root),
            values=_settings_values(cfg, secrets),
            service_toggles=list(_service_toggles()),
        )


def _service_toggles() -> tuple[str, ...]:
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    return tuple(
        category.name
        for category in sorted(all_categories(), key=lambda category: (category.priority, category.name))
        if not category.always_on
    )


def update_settings(root: Path, update: SettingsUpdate) -> SettingsView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:settings"):
        if config_revision(root) != update.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        values = update.values
        if set(values.services) != set(_service_toggles()):
            raise DesiredStateValidationError("service toggle set is invalid")
        cfg = load_config(config_path(root))
        candidate = cfg.model_copy(
            update={
                "domain": values.domain,
                "email": values.email,
                "timezone": values.timezone,
                "services": ServicesConfig.model_validate(values.services),
                "notifications": cfg.notifications.model_copy(
                    update={
                        "deploy_ntfy_url": values.deploy_ntfy_url,
                        "smtp": cfg.notifications.smtp.model_copy(
                            update={
                                "mode": values.smtp_mode,
                                "host": values.smtp_host,
                                "port": values.smtp_port,
                                "starttls": values.smtp_starttls,
                                "username": values.smtp_username,
                                "password_secret": (
                                    DEFAULT_SMTP_PASSWORD_SECRET
                                    if values.smtp_mode == "external" and values.smtp_username
                                    else ""
                                ),
                                "from_address": values.smtp_from_address,
                            }
                        ),
                    }
                ),
                "ssh": cfg.ssh.model_copy(
                    update={
                        "auth_method": values.ssh_auth,
                        "key_file": values.ssh_key_file,
                    }
                ),
                "proxmox": cfg.proxmox.model_copy(
                    update={
                        "api_url": values.proxmox_api_url,
                        "control_host": values.proxmox_control_host,
                        "ssh": cfg.proxmox.ssh.model_copy(
                            update={
                                "user": values.proxmox_ssh_user,
                                "port": values.proxmox_ssh_port,
                                "key_file": values.proxmox_ssh_key_file,
                                "connect_timeout": values.proxmox_ssh_connect_timeout,
                                "command_timeout": values.proxmox_ssh_command_timeout,
                                "retries": values.proxmox_ssh_retries,
                            }
                        ),
                        "node": values.proxmox_node,
                        "lxc_storage": values.proxmox_storage,
                        "lxc_template_datastore": values.proxmox_template_datastore,
                        "lxc_template_url": values.proxmox_template_url,
                        "lxc_template_checksum": values.proxmox_template_checksum,
                        "tls_ca_file": values.proxmox_tls_ca_file,
                        "provision_machines": values.proxmox_provision_machines,
                    }
                ),
                "dns": cfg.dns.model_copy(
                    update={
                        "provider": values.dns_provider,
                        "public_ip": values.dns_public_ip,
                        "proxy_enabled": values.dns_proxy,
                    }
                ),
                "network": cfg.network.model_copy(
                    update={
                        "expose_via_internet": values.expose_internet,
                        "container_ipv4_cidr": values.container_ipv4_cidr,
                        "container_network_prefix": values.container_network_prefix,
                    }
                ),
            }
        )
        validated = Config.model_validate(candidate.model_dump(mode="python"))
        secret_path = secrets_path(root)
        current_secrets = load_secrets_plaintext(secret_path) if secret_path.exists() else {}
        updated_secrets = dict(current_secrets)
        smtp_password = update.smtp_password
        if smtp_password:
            if not smtp_password.strip():
                raise SMTPSettingsValidationError("config", "SMTP password must not be blank")
            smtp = validated.notifications.smtp
            if smtp.mode != "external" or not smtp.username or not smtp.password_secret:
                raise SMTPSettingsValidationError(
                    "config",
                    "SMTP password requires authenticated external SMTP",
                )
            allowed = {spec.name for spec in get_required_secrets(validated) if spec.tier is SecretTier.USER}
            if smtp.password_secret not in allowed:
                raise SMTPSettingsValidationError(
                    "config",
                    "SMTP password secret is not user-configurable",
                )
            updated_secrets[smtp.password_secret] = smtp_password
        if validated.notifications.smtp.mode == "external":
            try:
                transport = resolve_smtp_transport(validated, updated_secrets)
            except ValueError as exc:
                raise SMTPSettingsValidationError("config", str(exc)) from exc
            if transport is None:
                raise SMTPSettingsValidationError(
                    "config",
                    "external SMTP transport is unavailable",
                )
            probe = probe_smtp_transport(transport)
            if not probe.ok:
                raise SMTPSettingsValidationError(
                    probe.stage,
                    f"SMTP verification failed during {probe.stage}: {probe.detail}",
                )
        secret_changed = updated_secrets != current_secrets
        if secret_changed:
            save_secrets_plaintext(updated_secrets, secret_path)
        try:
            save_config(validated, config_path(root))
            save_local_config(validated, root)
        except Exception as original_error:
            rollback_errors: list[Exception] = []
            if secret_changed:
                try:
                    save_secrets_plaintext(current_secrets, secret_path)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            try:
                save_config(cfg, config_path(root))
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            try:
                save_local_config(cfg, root)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            if rollback_errors:
                raise DesiredStateValidationError(
                    "Settings write failed and automatic rollback was incomplete; explicit recovery is required"
                ) from original_error
            raise
    return read_settings_view(root)


def _machine_dependencies(root: Path, cfg: Config) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_node_map
    from toolkit.core.projects.placement import project_node

    project_catalog = any((root / "toolkit" / "services").glob("*/service.yaml")) or any(
        (root / "services").glob("*/service.yaml")
    )
    catalog = load_service_catalog(root if project_catalog else None)
    services: dict[str, list[str]] = {machine_id: [] for machine_id in cfg.machines}
    for service, machine_id in service_node_map(cfg, catalog).items():
        services.setdefault(machine_id, []).append(service)
    projects: dict[str, list[str]] = {machine_id: [] for machine_id in cfg.machines}
    for project in cfg.projects.entries:
        projects.setdefault(project_node(cfg, project), []).append(project.subdomain)
    return services, projects


def _machine_blockers(
    cfg: Config,
    machine_id: str,
    services: list[str],
    projects: list[str],
) -> list[str]:
    machine = cfg.machines[machine_id]
    blockers: list[str] = []
    if machine_id == cfg.control_node:
        blockers.append("control machine")
    if machine.enabled:
        blockers.append("machine is enabled")
    if machine.managed:
        blockers.append("managed Proxmox resource requires approved retirement")
    if services:
        blockers.append(f"hosts {len(services)} service(s)")
    if projects:
        blockers.append(f"hosts {len(projects)} project(s)")
    return blockers


def _machine_retirement_blockers(
    cfg: Config,
    machine_id: str,
    services: list[str],
    projects: list[str],
) -> list[str]:
    machine = cfg.machines.get(machine_id)
    if machine is None:
        return ["machine does not exist"]
    blockers: list[str] = []
    if machine_id == cfg.control_node:
        blockers.append("control machine cannot be retired")
    if not machine.enabled:
        blockers.append("machine is already disabled")
    if not machine.managed:
        blockers.append("external machines are removed without infrastructure retirement")
    if services:
        blockers.append(f"machine hosts {len(services)} service(s)")
    if projects:
        blockers.append(f"machine hosts {len(projects)} project(s)")
    return blockers


def machine_retirement_blockers(root: Path, cfg: Config, machine_id: str) -> list[str]:
    """Return reasons a managed machine cannot be safely retired."""
    services, projects = _machine_dependencies(root, cfg)
    return _machine_retirement_blockers(
        cfg,
        machine_id,
        services.get(machine_id, []),
        projects.get(machine_id, []),
    )


def _machines_view(root: Path, cfg: Config, revision: str) -> MachinesView:
    from toolkit.core.machines import load_machine_templates

    services, projects = _machine_dependencies(root, cfg)
    machines: list[MachineView] = []
    for machine_id in cfg.machines:
        machine_services = sorted(services.get(machine_id, []))
        machine_projects = sorted(projects.get(machine_id, []))
        blockers = _machine_blockers(cfg, machine_id, machine_services, machine_projects)
        retirement_blockers = _machine_retirement_blockers(
            cfg,
            machine_id,
            machine_services,
            machine_projects,
        )
        machines.append(
            MachineView(
                machine_id=machine_id,
                spec=cfg.machines[machine_id],
                services=machine_services,
                projects=machine_projects,
                can_remove=not blockers,
                removal_blockers=blockers,
                can_retire=not retirement_blockers,
                retirement_blockers=retirement_blockers,
            )
        )
    machines.sort(key=lambda item: (item.spec.startup_order, item.machine_id))
    return MachinesView(
        revision=revision,
        machines=machines,
        templates=[
            MachineTemplateView(template_id=template_id, spec=spec)
            for template_id, spec in sorted(load_machine_templates(root).items())
        ],
    )


def read_machines_view(root: Path) -> MachinesView:
    root = root.resolve()
    with configuration_lock(root):
        return _machines_view(root, load_config(config_path(root)), config_revision(root))


def _validated_machine_config(cfg: Config, machines: dict[str, MachineSpec]) -> Config:
    try:
        return Config.model_validate({**cfg.model_dump(mode="python"), "machines": machines})
    except (ValueError, TypeError) as exc:
        raise DesiredStateValidationError("machine desired state is invalid") from exc


def create_machine(root: Path, request: MachineCreate) -> MachinesView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:machine-create"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        if request.machine_id in cfg.machines:
            raise DesiredStateConflictError("machine already exists")
        machines = {**cfg.machines, request.machine_id: request.spec}
        save_config(_validated_machine_config(cfg, machines), config_path(root))
    return read_machines_view(root)


def update_machine(root: Path, machine_id: str, request: MachineUpdate) -> MachinesView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:machine-update"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        if machine_id not in cfg.machines:
            raise DesiredStateConflictError("machine does not exist")
        current = cfg.machines[machine_id]
        if current.managed and (
            not request.spec.managed
            or not request.spec.enabled
            or request.spec.kind != current.kind
            or request.spec.vmid != current.vmid
        ):
            raise DesiredStateValidationError(
                "managed machine ownership, kind, VMID, and existence require approved retirement or replacement"
            )
        machines = {**cfg.machines, machine_id: request.spec}
        save_config(_validated_machine_config(cfg, machines), config_path(root))
    return read_machines_view(root)


def remove_machine(root: Path, machine_id: str, request: MachineRemove) -> MachinesView:
    root = root.resolve()
    if request.machine_id != machine_id or request.confirmation != machine_id:
        raise DesiredStateValidationError("machine removal confirmation does not match")
    with configuration_mutation(root, "desired-state:machine-remove"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        if machine_id not in cfg.machines:
            raise DesiredStateConflictError("machine does not exist")
        services, projects = _machine_dependencies(root, cfg)
        blockers = _machine_blockers(cfg, machine_id, services.get(machine_id, []), projects.get(machine_id, []))
        if blockers:
            raise DesiredStateValidationError("machine cannot be removed: " + "; ".join(blockers))
        machines = {key: value for key, value in cfg.machines.items() if key != machine_id}
        save_config(_validated_machine_config(cfg, machines), config_path(root))
    return read_machines_view(root)


def _project_view(cfg: Config, entry: ProjectEntry) -> ProjectView:
    from toolkit.core.projects.placement import project_node

    return ProjectView(
        name=entry.name,
        subdomain=entry.subdomain,
        auth_mode=entry.auth_mode,
        exposure=entry.exposure,
        description=entry.description,
        show_on_portal=entry.show_on_portal,
        docker_image=entry.docker_image,
        container_port=entry.container_port,
        placement=entry.placement,
        node=project_node(cfg, entry),
        health_endpoint=entry.health_endpoint,
        read_only=entry.read_only,
        database_service=entry.database_service,
        upstream=entry.upstream,
    )


def read_projects_view(root: Path) -> ProjectsView:
    root = root.resolve()
    with configuration_lock(root):
        cfg = load_config(config_path(root))
        from toolkit.core.manifest.placement import manifest_node
        from toolkit.core.projects.database import project_database_providers

        return ProjectsView(
            revision=config_revision(root),
            domain=cfg.domain,
            available_placements=[
                ProjectPlacementOption(selector=selector, node=node, kind=kind)
                for selector, node, kind in project_placement_options(cfg)
            ],
            available_databases=[
                ProjectDatabaseOption(
                    service=manifest.name,
                    label=manifest.label,
                    engine=manifest.database_provider.engine,
                    node=manifest_node(cfg, manifest),
                )
                for manifest in project_database_providers(cfg)
                if manifest.database_provider is not None
            ],
            projects=[_project_view(cfg, entry) for entry in cfg.projects.entries],
        )


def create_project(root: Path, request: ProjectCreate) -> ProjectsView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:project-create"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        project = request.project
        if any(entry.subdomain == project.subdomain for entry in cfg.projects.entries):
            raise DesiredStateConflictError("project already exists")
        from toolkit.core.compose.port_conflict import check_container_name, check_port_conflict
        from toolkit.core.projects.placement import project_node

        entry = ProjectEntry.model_validate(project.model_dump(mode="python"))
        try:
            node = project_node(cfg, entry)
        except ValueError as exc:
            raise DesiredStateValidationError(str(exc)) from exc
        conflicts = check_port_conflict(node, entry.container_port, cfg)
        if conflicts:
            raise DesiredStateValidationError("project port conflicts with managed desired state")
        if check_container_name(entry.subdomain, cfg, root=root):
            raise DesiredStateValidationError("project name conflicts with managed desired state")
        updated = cfg.model_copy(deep=True)
        updated.projects.entries.append(entry)
        save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
    return read_projects_view(root)


def remove_project(root: Path, request: ProjectRemove) -> ProjectsView:
    root = root.resolve()
    with configuration_mutation(root, "desired-state:project-remove"):
        if config_revision(root) != request.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        cfg = load_config(config_path(root))
        entries = [entry for entry in cfg.projects.entries if entry.subdomain != request.subdomain]
        if len(entries) == len(cfg.projects.entries):
            raise DesiredStateConflictError("project does not exist")
        updated = cfg.model_copy(deep=True)
        updated.projects.entries = entries
        save_config(Config.model_validate(updated.model_dump(mode="python")), config_path(root))
    return read_projects_view(root)
