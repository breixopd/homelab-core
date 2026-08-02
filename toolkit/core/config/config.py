from __future__ import annotations

import ipaddress
import math
import os
import re
import tempfile
import warnings
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from toolkit.core.config.storage import config_path, env_path
from toolkit.core.config.validators import validate_email, validate_ipv4, validate_port, validate_url
from toolkit.core.machines import MachineSpec, load_default_machines
from toolkit.core.machines.models import validate_machine_id

DEFAULT_PROXMOX_NODE = "pve"
DEFAULT_LXC_TEMPLATE_URL = "http://download.proxmox.com/images/system/debian-12-standard_12.12-1_amd64.tar.zst"
DEFAULT_LXC_TEMPLATE_SHA256 = "ff5c55cba730fc1e93bc7de3e0ea4aecb05c692094009cfcf2999973a56f15e5"
DEFAULT_SMTP_PASSWORD_SECRET = "OPERATOR_SMTP_PASSWORD"
_SERVICE_SETTING_OWNER = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SERVICE_SETTING_KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
ServiceSettingScalar = StrictBool | StrictInt | StrictFloat | StrictStr


class ToolkitState(StrEnum):
    UNINITIALIZED = "uninitialized"  # No config.yaml exists
    CONFIG_ONLY = "config_only"  # config.yaml exists, not yet generated
    READY = "ready"  # Config + generated artifacts exist


def get_state(root: Path | None = None) -> ToolkitState:
    if not config_path(root).exists():
        return ToolkitState.UNINITIALIZED
    config = load_config(config_path(root))
    if not env_path(config.control_node, root).exists():
        return ToolkitState.CONFIG_ONLY
    return ToolkitState.READY


class NetworkConfig(BaseModel):
    """Global ingress and non-HTTP network controls."""

    model_config = ConfigDict(extra="forbid")

    expose_via_internet: bool = True
    mesh_ipv4_cidr: str = "100.64.0.0/10"
    mesh_ipv6_cidr: str = "fd7a:115c:a1e0::/48"
    container_ipv4_cidr: str = "172.31.0.0/17"
    container_network_prefix: int = Field(default=28, ge=24, le=29)
    # Public protocol exposure is compiled from the owning service manifests.
    mail_public_access: bool = True
    dns_public_access: bool = True

    @field_validator("mesh_ipv4_cidr")
    @classmethod
    def valid_mesh_ipv4_cidr(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError("mesh IPv4 CIDR must be a canonical network") from exc
        supported = ipaddress.IPv4Network("100.64.0.0/10")
        if not isinstance(network, ipaddress.IPv4Network) or not network.subnet_of(supported):
            raise ValueError("mesh IPv4 CIDR must be a subnet of 100.64.0.0/10")
        return str(network)

    @field_validator("mesh_ipv6_cidr")
    @classmethod
    def valid_mesh_ipv6_cidr(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError("mesh IPv6 CIDR must be a canonical network") from exc
        supported = ipaddress.IPv6Network("fd7a:115c:a1e0::/48")
        if not isinstance(network, ipaddress.IPv6Network) or not network.subnet_of(supported):
            raise ValueError("mesh IPv6 CIDR must be a subnet of fd7a:115c:a1e0::/48")
        return str(network)

    @field_validator("container_ipv4_cidr")
    @classmethod
    def valid_container_ipv4_cidr(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError("container IPv4 CIDR must be a canonical network") from exc
        private_ranges = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if not isinstance(network, ipaddress.IPv4Network) or not any(
            network.subnet_of(private) for private in private_ranges
        ):
            raise ValueError("container IPv4 CIDR must be inside an RFC1918 private range")
        return str(network)

    @model_validator(mode="after")
    def container_subnet_must_fit_pool(self) -> NetworkConfig:
        pool = ipaddress.ip_network(self.container_ipv4_cidr)
        if self.container_network_prefix <= pool.prefixlen:
            raise ValueError("container network prefix must create multiple subnets inside the container IPv4 pool")
        return self


class FleetConfig(BaseModel):
    """External VPS / fleet nodes — mesh via Headscale (not FRP)."""

    model_config = ConfigDict(extra="forbid")

    # Tags applied to preauth keys during fleet onboard (VPS/NAS — shows as tagged-devices).
    headscale_tags: list[str] = Field(default_factory=lambda: ["tag:fleet-external"])
    # ACL tagOwners principal (Headscale user@ form). Defaults from config email local-part.
    headscale_tag_owner: str = ""
    # Advertise the mesh-router machine's declared network to mesh clients.
    mesh_subnet_router: bool = True
    mesh_router_tag: str = "tag:homelab-router"


_PROJECT_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")


class ProjectEntry(BaseModel):
    """A digest-pinned container project managed through the desired state."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    subdomain: str
    auth_mode: Literal["forward_auth", "native"]
    exposure: Literal["public", "private"]
    description: str = ""
    show_on_portal: bool = True

    docker_image: str
    container_port: int = Field(default=80, ge=1, le=65_535)
    placement: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    health_endpoint: str = ""
    read_only: bool = True

    # Optional service manifest that provisions a dedicated project database.
    database_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")

    @field_validator("subdomain")
    @classmethod
    def valid_subdomain(cls, value: str) -> str:
        if not _PROJECT_SUBDOMAIN_RE.fullmatch(value):
            raise ValueError("project subdomain must be a lowercase DNS label")
        return value

    @field_validator("docker_image")
    @classmethod
    def immutable_image(cls, value: str) -> str:
        from toolkit.core.images.references import parse_immutable_image_reference

        try:
            parse_immutable_image_reference(value)
        except ValueError as exc:
            raise ValueError("project image must include an immutable tag and sha256 digest") from exc
        return value

    @field_validator("health_endpoint")
    @classmethod
    def valid_health_endpoint(cls, value: str) -> str:
        if value and (not value.startswith("/") or any(token in value for token in ("?", "#", "\\", ".."))):
            raise ValueError("project health endpoint must be an absolute path")
        return value

    @property
    def upstream(self) -> str:
        return f"{self.subdomain}:{self.container_port}"


class ProjectsConfig(BaseModel):
    entries: list[ProjectEntry] = Field(default_factory=list)


class ServicesConfig(BaseModel):
    """Strict category overrides discovered from category plugins."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, StrictBool] = Field(init=False)

    def enabled(self, name: str) -> bool:
        return bool(self.model_dump().get(name, True))


def _default_services_config() -> ServicesConfig:
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    return ServicesConfig.model_validate({category.name: True for category in all_categories()})


class DNSConfig(BaseModel):
    provider: str = "cloudflare"
    public_ip: str = ""
    # Cloudflare orange-cloud proxy (WAF/CDN). Requires zone SSL mode "full" with origin TLS (Caddy).
    proxy_enabled: bool = True
    verification_resolvers: list[str] = Field(
        default_factory=lambda: ["1.1.1.1", "8.8.8.8"],
        max_length=4,
    )

    @field_validator("verification_resolvers")
    @classmethod
    def valid_verification_resolvers(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError("DNS verification resolvers must be IP addresses") from exc
        return values


class ImageRegistryAuthConfig(BaseModel):
    """Optional pull-only registry credential stored in the encrypted secret store."""

    username: str = Field(default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?$")
    token_secret: str = Field(default="", pattern=r"^(?:[A-Z][A-Z0-9_]{0,127})?$")

    @model_validator(mode="after")
    def complete_pair(self) -> ImageRegistryAuthConfig:
        if bool(self.username) != bool(self.token_secret):
            raise ValueError("image registry auth requires both username and token_secret")
        return self


class ImagesConfig(BaseModel):
    """Custom image source and registry coordinates."""

    registry: str = Field(
        default="ghcr.io/breixopd",
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$",
    )
    tag: str = Field(default="auto", pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    source: Literal["auto", "registry", "local"] = "auto"
    auth: ImageRegistryAuthConfig = Field(default_factory=ImageRegistryAuthConfig)

    @field_validator("registry")
    @classmethod
    def registry_has_no_url_scheme(cls, value: str) -> str:
        if "://" in value:
            raise ValueError("image registry must be a Docker registry name, not a URL")
        return value.rstrip("/")


class SMTPNotificationConfig(BaseModel):
    """SMTP transport policy for operator notifications."""

    mode: Literal["auto", "external", "disabled"] = "auto"
    host: str = Field(default="", max_length=253)
    port: int = Field(default=587, ge=1, le=65_535)
    starttls: bool = True
    username: str = Field(default="", max_length=320)
    password_secret: str = Field(default="", pattern=r"^(?:[A-Z][A-Z0-9_]{0,127})?$")
    from_address: str = ""

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if any(character.isspace() for character in value) or "://" in value:
            raise ValueError("SMTP host must be a hostname or IP address without a URL scheme")
        return value

    @field_validator("from_address")
    @classmethod
    def valid_from_address(cls, value: str) -> str:
        return validate_email(value)

    @model_validator(mode="after")
    def complete_external_transport(self) -> SMTPNotificationConfig:
        if self.mode == "external" and not self.host:
            raise ValueError("external SMTP mode requires host")
        if self.mode == "external" and not self.starttls and self.port != 465:
            raise ValueError("external SMTP requires STARTTLS or implicit TLS on port 465")
        if self.mode == "external" and self.starttls and self.port == 465:
            raise ValueError("SMTP port 465 requires implicit TLS with STARTTLS disabled")
        if bool(self.username) != bool(self.password_secret):
            raise ValueError("SMTP authentication requires both username and password_secret")
        return self


class NotificationsConfig(BaseModel):
    """External notification targets (deploy completion, etc.)."""

    # Full ntfy topic URL, e.g. https://ntfy.sh/my-homelab-topic
    deploy_ntfy_url: str = ""
    smtp: SMTPNotificationConfig = Field(default_factory=SMTPNotificationConfig)

    # Update check notification settings
    update_check_email: bool = True
    update_check_ntfy: bool = True
    update_check_schedule: str = "0 3 * * *"

    # Health report schedule (cron expression, default 3 AM daily)
    health_report_schedule: str = "0 3 * * *"


class ProxmoxSSHConfig(BaseModel):
    """Key-only administrative SSH connection to the Proxmox control host."""

    model_config = ConfigDict(extra="forbid")

    user: str = Field(default="root", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    port: int = Field(default=22, ge=1, le=65_535)
    key_file: str = Field(default="", max_length=4_096)
    connect_timeout: int = Field(default=30, ge=1, le=300)
    command_timeout: int = Field(default=120, ge=5, le=7_200)
    retries: int = Field(default=3, ge=1, le=10)

    @field_validator("key_file")
    @classmethod
    def valid_key_file(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("Proxmox SSH key path contains invalid characters")
        return value


class ProxmoxConfig(BaseModel):
    """Typed provider and host settings for Proxmox reconciliation."""

    model_config = ConfigDict(extra="forbid")

    api_url: str = ""
    control_host: str = ""
    node: str = Field(default=DEFAULT_PROXMOX_NODE, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    lxc_storage: str = Field(default="local", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    lxc_template_datastore: str = Field(default="local", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    lxc_template_url: str = DEFAULT_LXC_TEMPLATE_URL
    lxc_template_checksum: str = Field(
        default=DEFAULT_LXC_TEMPLATE_SHA256,
        pattern=r"^[a-f0-9]{64}$",
    )
    provision_machines: bool = True
    ssh_public_key: str = ""
    tls_ca_file: str = Field(default="", max_length=4_096)
    ssh: ProxmoxSSHConfig = Field(default_factory=ProxmoxSSHConfig)

    _validate_urls = field_validator("api_url", "lxc_template_url")(validate_url)

    @field_validator("tls_ca_file")
    @classmethod
    def valid_tls_ca_file(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("Proxmox CA file path contains invalid characters")
        return value

    @field_validator("control_host")
    @classmethod
    def valid_control_host(cls, value: str) -> str:
        if not value:
            return value
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        if len(value) > 253:
            raise ValueError("Proxmox control host is too long")
        labels = value.rstrip(".").split(".")
        if not all(
            label and len(label) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("Proxmox control host must be a hostname or IP address without a scheme or port")
        return value.rstrip(".")

    @property
    def resolved_control_host(self) -> str:
        if self.control_host:
            return self.control_host
        from urllib.parse import urlparse

        return urlparse(self.api_url).hostname or ""


class SSHConfig(BaseModel):
    """Shared guest identity and transport policy; endpoints live in machine plugins."""

    model_config = ConfigDict(extra="forbid")

    auth_method: Literal["key", "password"] = "key"
    key_file: str = ""
    password: str = ""
    connect_timeout: int = 30
    command_timeout: int = 120
    retries: int = 3


class HostCapacityConfig(BaseModel):
    """Deploy tuning — defaults query Proxmox host when provisioning remotely."""

    use_proxmox_host: bool = True
    proxmox_host: str = ""
    cpu_cores: int | None = None
    mem_total_mb: int | None = None
    load_threshold: float | None = None


class RuntimeConfig(BaseModel):
    puid: int | None = None
    pgid: int | None = None


class MediaMountConfig(BaseModel):
    type: Literal["nfs", "cifs", "local", "rclone"] = "rclone"
    server: str = ""
    path: str = ""
    mount_point: str = "/mnt/media"
    options: str = ""


class BackupsConfig(BaseModel):
    """Backup automation — off by default; enable when a backup target is configured."""

    enabled: bool = False
    # local = Kopia on infra LXC; remote = fleet backup-storage host (UI-driven).
    target: Literal["local", "remote"] = "local"
    # Name of external_hosts entry with backup-storage service (set by hosts UI).
    storage_host: str = ""


class MaintenanceConfig(BaseModel):
    """Scheduled maintenance policy applied to every deployed runtime node."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    daily_at: str = Field(default="03:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    image_update_scan: bool = True
    cert_warning_days: int = Field(default=14, ge=1, le=90)


class StorageConfig(BaseModel):
    media_mount: MediaMountConfig | None = None
    zfs_enabled: bool = False
    zfs_pool: str = "data"
    filesystem: str = "zfs"
    raid_level: str = "mirror"
    zfs_disk_list: str = "sda,sdb"
    raw_disks_gb: int = 0
    disk_count: int = 2
    zfs_overhead_pct: float = Field(default=2.0, ge=0, le=100)

    @property
    def usable_gb(self) -> int:
        """Usable capacity after RAID redundancy + filesystem overhead."""
        if self.raw_disks_gb <= 0 or self.disk_count <= 0:
            return 0
        raw = self.raw_disks_gb
        if self.raid_level == "mirror":
            usable = raw / self.disk_count
        elif self.raid_level == "raidz1":
            usable = raw * (self.disk_count - 1) / self.disk_count
        elif self.raid_level == "raidz2":
            usable = raw * (self.disk_count - 2) / self.disk_count
        elif self.raid_level == "stripe":
            usable = raw
        else:
            usable = raw
        overhead = self.zfs_overhead_pct if self.filesystem == "zfs" else 5.0
        usable *= 1 - overhead / 100
        return int(usable)


def external_host_default_services() -> list[str]:
    """Return manifest-declared defaults for a plain managed host."""
    from toolkit.core.infra.fleet_roles import plain_host_default_services

    return plain_host_default_services()


_EXTERNAL_HOST_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_EXTERNAL_HOST_SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
_EXTERNAL_HOST_GROUP_RE = re.compile(r"^[a-zA-Z0-9_-]*$")
_HEADSCALE_TAG_RE = re.compile(r"^tag:[a-zA-Z0-9_-]+$")


def external_host_selectable_services(kind: Literal["plain", "fleet"]) -> tuple[str, ...]:
    """Return the service IDs valid for a host kind."""
    from toolkit.core.infra.fleet_roles import services_for_kind

    return tuple(services_for_kind(kind))


class ExternalHost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ip: str
    kind: Literal["plain", "fleet"] = "plain"
    ssh_user: str = "root"
    ssh_port: int = 22
    services: list[str] = Field(default_factory=list)
    applied_services: list[str] = Field(default_factory=list)
    integrations: dict[str, dict[str, ServiceSettingScalar]] = Field(default_factory=dict)
    cluster_group: str = ""
    lldap_email: str = ""
    headscale_tags: list[str] = Field(default_factory=list)
    reconciled: bool = False
    last_reconcile_at: str = ""

    _validate_ip = field_validator("ip")(validate_ipv4)
    _validate_ssh_port = field_validator("ssh_port")(validate_port)
    _validate_lldap_email = field_validator("lldap_email")(validate_email)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _EXTERNAL_HOST_NAME_RE.fullmatch(value):
            raise ValueError(f"Invalid host name: {value}")
        return value

    @field_validator("ssh_user")
    @classmethod
    def _validate_ssh_user(cls, value: str) -> str:
        if not _EXTERNAL_HOST_SSH_USER_RE.fullmatch(value):
            raise ValueError(f"Invalid SSH user: {value}")
        return value

    @field_validator("cluster_group")
    @classmethod
    def _validate_cluster_group(cls, value: str) -> str:
        value = value.strip()
        if not _EXTERNAL_HOST_GROUP_RE.fullmatch(value):
            raise ValueError(f"Invalid fleet cluster group: {value}")
        return value

    @field_validator("headscale_tags")
    @classmethod
    def _validate_headscale_tags(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        invalid = [value for value in normalized if not _HEADSCALE_TAG_RE.fullmatch(value)]
        if invalid:
            raise ValueError(f"Invalid Headscale tag(s): {', '.join(invalid)}")
        return normalized

    @field_validator("last_reconcile_at")
    @classmethod
    def _validate_reconcile_timestamp(cls, value: str) -> str:
        if not value:
            return value
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid host reconciliation timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("Host reconciliation timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_host_contract(self) -> ExternalHost:
        self.services = list(dict.fromkeys(service.strip() for service in self.services if service.strip()))
        self.applied_services = list(
            dict.fromkeys(service.strip() for service in self.applied_services if service.strip())
        )
        allowed = external_host_selectable_services(self.kind)
        unknown = [service for service in [*self.services, *self.applied_services] if service not in allowed]
        if unknown:
            raise ValueError(f"Unknown {self.kind} host service(s): {', '.join(unknown)} (valid: {', '.join(allowed)})")
        from toolkit.core.infra.fleet_roles import FLEET_SERVICE_CATALOG

        catalog = {service.name: service for service in FLEET_SERVICE_CATALOG}
        stale = sorted(set(self.integrations) - set(self.services))
        if stale:
            raise ValueError(f"Integration settings provided for service(s) not selected: {', '.join(stale)}")
        selected = {name: catalog[name] for name in self.services}
        for name, service in selected.items():
            values = dict(self.integrations.get(name, {}))
            declared = {field.key: field for field in service.fields}
            unknown_fields = sorted(set(values) - set(declared))
            if unknown_fields:
                raise ValueError(f"Unknown {name} integration field(s): {', '.join(unknown_fields)}")
            normalized: dict[str, ServiceSettingScalar] = {}
            for key, field in declared.items():
                value = values.get(key, field.default)
                if value is None or (isinstance(value, str) and not value.strip()):
                    if field.required:
                        raise ValueError(f"{name}.{key} is required when the '{name}' service is selected")
                    continue
                valid_type = {
                    "boolean": type(value) is bool,
                    "integer": type(value) is int,
                    "number": type(value) in {int, float},
                    "path": isinstance(value, str),
                    "text": isinstance(value, str),
                }[field.type]
                if not valid_type:
                    raise ValueError(f"{name}.{key} must be a {field.type} value")
                if type(value) is float and not math.isfinite(value):
                    raise ValueError(f"{name}.{key} must be finite")
                if isinstance(value, str):
                    value = value.strip()
                    if len(value) > 4_096:
                        raise ValueError(f"{name}.{key} cannot exceed 4096 characters")
                    if any(ord(character) < 32 or ord(character) == 127 for character in value):
                        raise ValueError(f"{name}.{key} cannot contain control characters")
                    if field.type == "path":
                        segments = value.split("/")
                        if (
                            not value.startswith("/")
                            or value == "/"
                            or "//" in value
                            or any(segment in {".", ".."} for segment in segments)
                            or "\\" in value
                        ):
                            raise ValueError(f"{name}.{key} must be an absolute path without traversal")
                normalized[key] = value
            if normalized:
                self.integrations[name] = normalized
            else:
                self.integrations.pop(name, None)
        if self.kind == "plain" and (self.cluster_group or self.lldap_email or self.headscale_tags):
            raise ValueError("Fleet enrollment metadata requires kind='fleet'")
        if self.reconciled != bool(self.last_reconcile_at):
            raise ValueError("Reconciled host state and reconciliation timestamp must be set together")
        if self.reconciled and set(self.applied_services) != set(self.services):
            raise ValueError("Reconciled hosts must record the current services as applied")
        return self

    def integration_value(
        self,
        service: str,
        field: str,
        default: ServiceSettingScalar | None = None,
    ) -> ServiceSettingScalar | None:
        """Return one validated service-owned managed-host setting."""
        return self.integrations.get(service, {}).get(field, default)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(
        default="localhost",
        pattern=r"^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$|^localhost$",
    )
    email: str = ""
    owner_username: str = Field(default="", pattern=r"^$|^[a-z0-9][a-z0-9._-]{0,31}$")
    timezone: str = "Europe/Madrid"
    # Overrides SSO_USER_PASSWORD during generation and deploy initialization.
    # Use this for the owner LLDAP/Authelia password across SSO-enabled services.
    owner_password: str = ""
    services: ServicesConfig = Field(default_factory=_default_services_config)
    service_settings: dict[str, dict[str, ServiceSettingScalar]] = Field(default_factory=dict)
    network: NetworkConfig = Field(default_factory=lambda: NetworkConfig())
    fleet: FleetConfig = Field(default_factory=lambda: FleetConfig())
    dns: DNSConfig = Field(default_factory=lambda: DNSConfig())
    images: ImagesConfig = Field(default_factory=lambda: ImagesConfig())
    notifications: NotificationsConfig = Field(default_factory=lambda: NotificationsConfig())
    proxmox: ProxmoxConfig = Field(default_factory=lambda: ProxmoxConfig())
    machines: dict[str, MachineSpec] = Field(default_factory=load_default_machines)
    ssh: SSHConfig = Field(default_factory=lambda: SSHConfig())
    host_capacity: HostCapacityConfig = Field(default_factory=lambda: HostCapacityConfig())
    runtime: RuntimeConfig = Field(default_factory=lambda: RuntimeConfig())
    storage: StorageConfig = Field(default_factory=lambda: StorageConfig())
    backups: BackupsConfig = Field(default_factory=lambda: BackupsConfig())
    projects: ProjectsConfig = Field(default_factory=lambda: ProjectsConfig())
    maintenance: MaintenanceConfig = Field(default_factory=lambda: MaintenanceConfig())
    external_hosts: list[ExternalHost] = Field(default_factory=list)

    _validate_email = field_validator("email")(validate_email)

    @field_validator("owner_username")
    @classmethod
    def _validate_owner_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"admin", "ldap-bind"}:
            raise ValueError("owner_username is reserved for an LLDAP service account")
        return normalized

    @field_validator("service_settings")
    @classmethod
    def _validate_service_settings(
        cls,
        values: dict[str, dict[str, ServiceSettingScalar]],
    ) -> dict[str, dict[str, ServiceSettingScalar]]:
        if len(values) > 512:
            raise ValueError("service settings contain too many service entries")
        for service, settings in values.items():
            if not _SERVICE_SETTING_OWNER.fullmatch(service):
                raise ValueError("service settings contain an invalid service identifier")
            if len(settings) > 32:
                raise ValueError(f"service settings for {service!r} contain too many entries")
            for key, value in settings.items():
                if not _SERVICE_SETTING_KEY.fullmatch(key):
                    raise ValueError("service settings contain an invalid setting identifier")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("service setting numbers must be finite")
                if isinstance(value, str) and len(value) > 4_096:
                    raise ValueError("service setting text is too long")
        return values

    @model_validator(mode="after")
    def _validate_configuration_invariants(self) -> Config:
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        categories = {category.name: category for category in all_categories()}
        configured_categories = self.services.model_dump()
        unknown_categories = sorted(set(configured_categories) - set(categories))
        if unknown_categories:
            raise ValueError(f"unknown service categories: {', '.join(unknown_categories)}")
        disabled_always_on = sorted(
            name for name, enabled in configured_categories.items() if not enabled and categories[name].always_on
        )
        if disabled_always_on:
            raise ValueError(f"always-on service categories cannot be disabled: {', '.join(disabled_always_on)}")
        disabled_dependencies = sorted(
            f"{category.name}->{dependency}"
            for category in categories.values()
            if category.always_on or configured_categories.get(category.name, True)
            for dependency in category.depends_on()
            if not (categories[dependency].always_on or configured_categories.get(dependency, True))
        )
        if disabled_dependencies:
            rendered_dependencies = ", ".join(disabled_dependencies)
            raise ValueError(f"enabled service categories have disabled dependencies: {rendered_dependencies}")
        for machine_id in self.machines:
            validate_machine_id(machine_id)
        vmids = [machine.vmid for machine in self.machines.values() if machine.managed]
        if len(vmids) != len(set(vmids)):
            raise ValueError("machine VMIDs must be unique")
        addresses = [machine.address for machine in self.machines.values()]
        if len(addresses) != len(set(addresses)):
            raise ValueError("machine addresses must be unique")
        hostnames = [machine.hostname for machine in self.machines.values()]
        if len(hostnames) != len(set(hostnames)):
            raise ValueError("machine hostnames must be unique")
        control_count = sum(machine.enabled and "control" in machine.labels for machine in self.machines.values())
        if control_count != 1:
            raise ValueError("exactly one enabled machine must have the control label")
        mesh_ipv4 = ipaddress.ip_network(self.network.mesh_ipv4_cidr)
        mesh_overlapping = sorted(
            machine_id
            for machine_id, machine in self.machines.items()
            if machine.enabled
            and ipaddress.ip_network(f"{machine.address}/{machine.cidr}", strict=False).overlaps(mesh_ipv4)
        )
        if mesh_overlapping:
            raise ValueError(f"machine network overlaps the mesh IPv4 pool for: {', '.join(mesh_overlapping)}")
        container_ipv4 = ipaddress.ip_network(self.network.container_ipv4_cidr)
        container_overlapping = sorted(
            machine_id
            for machine_id, machine in self.machines.items()
            if machine.enabled
            and ipaddress.ip_network(f"{machine.address}/{machine.cidr}", strict=False).overlaps(container_ipv4)
        )
        if container_overlapping:
            raise ValueError(
                f"machine network overlaps the container IPv4 pool for: {', '.join(container_overlapping)}"
            )
        if any(machine.resource_limits for machine in self.machines.values()):
            from toolkit.core.config.service_metadata import (
                get_service_resource_requirements,
                managed_runtime_service_names,
            )

            managed_services = managed_runtime_service_names()
            for machine_id, machine in self.machines.items():
                unknown_limits = sorted(set(machine.resource_limits) - managed_services)
                if unknown_limits:
                    raise ValueError(
                        f"machine {machine_id!r} has resource limits for unknown services: {', '.join(unknown_limits)}"
                    )
                for service, limit in machine.resource_limits.items():
                    memory_floor_mb, cpu_floor = get_service_resource_requirements(service)
                    if limit.memory_mb < memory_floor_mb or limit.cpus < cpu_floor:
                        raise ValueError(
                            f"machine {machine_id!r} resource limit for {service!r} is below its manifest floor"
                        )
        host_names = [host.name for host in self.external_hosts]
        if len(host_names) != len(set(host_names)):
            raise ValueError("Managed host names must be unique")
        project_subdomains = [project.subdomain for project in self.projects.entries]
        if len(project_subdomains) != len(set(project_subdomains)):
            raise ValueError("Project subdomains must be unique")
        database_errors: list[str] = []
        database_projects = [project for project in self.projects.entries if project.database_service]
        if database_projects:
            from toolkit.core.manifest.catalog import load_service_catalog
            from toolkit.core.manifest.routes import service_is_enabled

            catalog = load_service_catalog()
            for project in database_projects:
                try:
                    provider = catalog.require(project.database_service)
                except KeyError:
                    database_errors.append(
                        f"project {project.subdomain!r} references unknown database service "
                        f"{project.database_service!r}"
                    )
                    continue
                if provider.database_provider is None:
                    database_errors.append(
                        f"service {project.database_service!r} is not a managed project database provider"
                    )
                elif not service_is_enabled(self, provider, catalog):
                    database_errors.append(
                        f"project {project.subdomain!r} database service {project.database_service!r} is disabled"
                    )
        if database_errors:
            raise ValueError("; ".join(database_errors))
        from toolkit.core.projects.placement import project_node

        placement_errors: list[str] = []
        for project in self.projects.entries:
            try:
                project_node(self, project)
            except ValueError as exc:
                placement_errors.append(str(exc))
        if placement_errors:
            raise ValueError("; ".join(placement_errors))
        if self.backups.target == "remote":
            storage_host = next(
                (host for host in self.external_hosts if host.name == self.backups.storage_host),
                None,
            )
            if storage_host is None or "backup-storage" not in storage_host.services:
                raise ValueError("Remote backups require a managed host with the backup-storage service")
        elif self.backups.storage_host:
            raise ValueError("Local backups cannot reference a remote storage host")
        if self.storage.zfs_enabled and "zfs" not in self.proxmox.lxc_storage.lower():
            warnings.warn(
                f"storage.zfs_enabled=True but proxmox.lxc_storage='{self.proxmox.lxc_storage}' "
                f"does not reference a ZFS dataset — LXCs will be created on non-ZFS storage.",
                stacklevel=2,
            )
        from toolkit.core.manifest.settings import validate_service_setting_overrides

        validate_service_setting_overrides(self)
        return self

    @property
    def is_multi_node(self) -> bool:
        return len(self.enabled_nodes) > 1

    @property
    def enabled_categories(self) -> list[str]:
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        categories = sorted(all_categories(), key=lambda category: (category.priority, category.name))
        return [category.name for category in categories if self.category_enabled(category.name)]

    def category_enabled(self, name: str) -> bool:
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        category = next((candidate for candidate in all_categories() if candidate.name == name), None)
        if category is None:
            raise KeyError(f"unknown service category: {name}")
        if category.always_on:
            return True
        return self.services.enabled(name)

    @property
    def enabled_nodes(self) -> list[str]:
        enabled = ((machine_id, machine) for machine_id, machine in self.machines.items() if machine.enabled)
        return [machine_id for machine_id, _ in sorted(enabled, key=lambda item: (item[1].startup_order, item[0]))]

    @property
    def control_node(self) -> str:
        controls = [
            machine_id
            for machine_id, machine in self.machines.items()
            if machine.enabled and "control" in machine.labels
        ]
        if len(controls) != 1:
            raise ValueError("exactly one enabled machine must have the control label")
        return controls[0]

    def node_ip(self, node: str) -> str:
        try:
            machine = self.machines[node]
        except KeyError as exc:
            raise KeyError(f"unknown machine: {node}") from exc
        if not machine.enabled:
            raise KeyError(f"disabled machine: {node}")
        return machine.address


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins on conflicts)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: Path) -> Config:
    """Load config.yaml and merge with config.local.yaml if present.

    config.local.yaml is gitignored and holds sensitive overrides such as the
    SSH public key injected into provisioned guests.
    """
    # Resolve the root directory regardless of whether path points to
    # the file itself (config.yaml) or the parent directory.
    root = path.parent if path.suffix else path
    yaml_path = root / "config.yaml"
    local_path = root / "config.local.yaml"

    raw: dict = {}
    if yaml_path.exists():
        raw = yaml.safe_load(yaml_path.read_text()) or {}
    if local_path.exists():
        local_raw = yaml.safe_load(local_path.read_text()) or {}
        raw = _deep_merge(raw, local_raw)

    return Config.model_validate(raw)


_LOCAL_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("owner_password",),
    ("proxmox", "ssh_public_key"),
    ("proxmox", "ssh", "key_file"),
    ("ssh", "key_file"),
    ("ssh", "password"),
)

_SENSITIVE_CONFIG_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("owner_password",),
        ("ssh", "password"),
    }
)
_REDACTED_CONFIG_VALUE = "<redacted>"


def _pop_path(data: dict, path: tuple[str, ...]) -> object | None:
    current = data
    for key in path[:-1]:
        nested = current.get(key)
        if not isinstance(nested, dict):
            return None
        current = nested
    return current.pop(path[-1], None)


def _get_path(data: dict, path: tuple[str, ...]) -> object | None:
    current: object = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _set_path(data: dict, path: tuple[str, ...], value: object) -> None:
    current = data
    for key in path[:-1]:
        nested = current.setdefault(key, {})
        if not isinstance(nested, dict):
            raise ValueError(f"local config path conflicts at {key}")
        current = nested
    current[path[-1]] = value


def config_path_is_sensitive(path: str | tuple[str, ...]) -> bool:
    """Return whether a dotted config path contains a plaintext credential."""
    parts = tuple(path.split(".")) if isinstance(path, str) else path
    return parts in _SENSITIVE_CONFIG_PATHS


def redact_sensitive_config(data: dict) -> dict:
    """Return a deep copy with configured plaintext credentials redacted."""
    import copy

    redacted = copy.deepcopy(data)
    for field_path in _SENSITIVE_CONFIG_PATHS:
        if _get_path(redacted, field_path):
            _set_path(redacted, field_path, _REDACTED_CONFIG_VALUE)
    return redacted


def save_config(config: Config, path: Path, *, actor: str = "cli") -> None:
    """Save the operator-owned config.yaml; plaintext credentials are excluded."""
    root = path.parent if path.suffix else path
    path = root / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_defaults=False)
    # Strip local-only fields from the tracked file.
    for field_path in _LOCAL_CONFIG_PATHS:
        _pop_path(data, field_path)
    content = yaml.dump(data, default_flow_style=False, sort_keys=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.yaml.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    # Audit every config save so the operator timeline shows who changed what.
    try:
        from toolkit.core.state.audit_log import AuditAction, audit

        audit(root, AuditAction.CONFIG_SAVE, actor=actor, ok=True, detail="config.yaml saved")
    except Exception:
        pass  # best-effort — never block a config save on audit logging


def save_local_config(config: Config, root: Path) -> None:
    """Save gitignored config.local.yaml with sensitive overrides."""
    local_path = root / "config.local.yaml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json")
    local_data: dict = {}
    for field_path in _LOCAL_CONFIG_PATHS:
        value = _get_path(data, field_path)
        if value:
            _set_path(local_data, field_path, value)
    if not local_data:
        local_path.unlink(missing_ok=True)
        return
    content = "# Local config overrides — gitignored, never committed.\n" + yaml.dump(
        local_data,
        default_flow_style=False,
        sort_keys=False,
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.local.yaml.", dir=local_path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, local_path)
    finally:
        temporary.unlink(missing_ok=True)
