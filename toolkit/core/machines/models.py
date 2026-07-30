"""Strict desired-state models for provisioned machines."""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MACHINE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_HOSTNAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_RUNTIME_SERVICE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_LINUX_USER_PATTERN = r"^(?:[a-z_][a-z0-9_-]{0,31})?$"
_SHA256_PATTERN = r"^(?:[a-f0-9]{64})?$"


class MachineDisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=r"^/[^\x00\r\n]*$")
    size_gb: int = Field(ge=1, le=1_000_000)
    datastore: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]{0,64}$")
    backup: bool = True

    @field_validator("path")
    @classmethod
    def valid_mount_path(cls, value: str) -> str:
        if value == "/":
            raise ValueError("data disk path cannot be root")
        if "//" in value or any(segment in {".", ".."} for segment in value.split("/")):
            raise ValueError("data disk path must be normalized")
        return value


class MachineResourceLimit(BaseModel):
    """Desired container resources for one runtime service on a machine."""

    model_config = ConfigDict(extra="forbid")

    memory_mb: int = Field(ge=64, le=4_194_304)
    cpus: float = Field(ge=0.05, le=256)


class MachineSpec(BaseModel):
    """One independently configurable machine instance."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["lxc", "vm"] = "lxc"
    provider: Literal["proxmox"] = "proxmox"
    enabled: bool = True
    managed: bool = True
    hostname: str = Field(pattern=_HOSTNAME_PATTERN)
    address: str
    vmid: int = Field(ge=100, le=999_999_999)
    description: str = Field(default="", max_length=500)
    labels: tuple[str, ...] = ()
    cores: int = Field(default=2, ge=1, le=256)
    memory_mb: int = Field(default=2_048, ge=512, le=4_194_304)
    root_disk_gb: int = Field(default=32, ge=4, le=1_000_000)
    root_datastore: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]{0,64}$")
    data_disks: tuple[MachineDisk, ...] = ()
    private_bridge: str = Field(default="vmbr1", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    public_bridge: str = Field(default="", pattern=r"^[A-Za-z0-9_.:-]{0,64}$")
    gateway: str
    cidr: int = Field(default=24, ge=1, le=32)
    startup_order: int = Field(default=20, ge=0, le=999)
    nesting: bool = True
    keyctl: bool = True
    fuse: bool = False
    template_file_id: str = ""
    admin_user: str = Field(default="", pattern=_LINUX_USER_PATTERN)
    ssh_user: str = Field(default="", pattern=_LINUX_USER_PATTERN)
    ssh_port: int = Field(default=22, ge=1, le=65_535)
    cloud_image_datastore: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]{0,64}$")
    cloud_image_format: Literal["", "qcow2", "raw"] = ""
    cloud_image_url: str = ""
    cloud_image_sha256: str = Field(default="", pattern=_SHA256_PATTERN)
    resource_limits: dict[str, MachineResourceLimit] = Field(default_factory=dict)

    @property
    def effective_ssh_user(self) -> str:
        """Resolve the login declared by this machine plugin."""
        return self.ssh_user or ("root" if self.kind == "lxc" else self.admin_user)

    @field_validator("address", "gateway")
    @classmethod
    def valid_ipv4(cls, value: str) -> str:
        parsed = ipaddress.ip_address(value)
        if parsed.version != 4:
            raise ValueError("machine networking requires IPv4")
        return str(parsed)

    @field_validator("labels")
    @classmethod
    def unique_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("machine labels must be unique")
        for value in values:
            if not value or not value.replace("-", "").replace("_", "").isalnum():
                raise ValueError("machine labels must be simple identifiers")
        return values

    @field_validator("cloud_image_url")
    @classmethod
    def valid_cloud_image_url(cls, value: str) -> str:
        if not value:
            return value
        if any(character.isspace() for character in value):
            raise ValueError("cloud image URL cannot contain whitespace")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("cloud image URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("cloud image URL must be an HTTP(S) URL without credentials")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("cloud image URL port is invalid")
        return value

    @field_validator("resource_limits")
    @classmethod
    def valid_resource_limit_names(cls, values: dict[str, MachineResourceLimit]) -> dict[str, MachineResourceLimit]:
        import re

        invalid = sorted(name for name in values if not re.fullmatch(_RUNTIME_SERVICE_PATTERN, name))
        if invalid:
            raise ValueError(f"invalid runtime service resource limit: {', '.join(invalid)}")
        return values

    @model_validator(mode="after")
    def valid_runtime_source(self) -> MachineSpec:
        if self.kind == "vm":
            if self.template_file_id:
                raise ValueError("template_file_id is only valid for LXC machines")
            if self.managed and not self.admin_user:
                raise ValueError("managed VMs require admin_user")
            if self.managed and not self.cloud_image_url:
                raise ValueError("managed VMs require cloud_image_url")
            if self.managed and not self.cloud_image_sha256:
                raise ValueError("managed VMs require cloud_image_sha256")
            if self.managed and not self.cloud_image_datastore:
                raise ValueError("managed VMs require cloud_image_datastore with Import content enabled")
            if self.managed and not self.cloud_image_format:
                raise ValueError("managed VMs require cloud_image_format")
            if self.enabled and not self.effective_ssh_user:
                raise ValueError("enabled VMs require admin_user or ssh_user")
        elif any(
            (
                self.admin_user,
                self.cloud_image_datastore,
                self.cloud_image_format,
                self.cloud_image_url,
                self.cloud_image_sha256,
            )
        ):
            raise ValueError("admin_user and cloud image fields are only valid for VM machines")
        network = ipaddress.ip_network(f"{self.address}/{self.cidr}", strict=False)
        if ipaddress.ip_address(self.gateway) not in network:
            raise ValueError("machine address and gateway must share the configured subnet")
        if self.address == self.gateway:
            raise ValueError("machine address must differ from its gateway")
        paths = [disk.path for disk in self.data_disks]
        if len(paths) != len(set(paths)):
            raise ValueError("machine data disk paths must be unique")
        max_cpus = max(round(self.cores * 0.95, 2), 0.1)
        for service, limit in self.resource_limits.items():
            if limit.memory_mb > self.memory_mb:
                raise ValueError(f"resource limit for {service!r} exceeds machine memory")
            if limit.cpus > max_cpus:
                raise ValueError(f"resource limit for {service!r} exceeds machine CPU capacity")
        return self


def validate_machine_id(machine_id: str) -> str:
    import re

    if not re.fullmatch(_MACHINE_ID_PATTERN, machine_id):
        raise ValueError(f"invalid machine id: {machine_id!r}")
    return machine_id
