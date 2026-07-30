"""dev-redis service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


def _parse_redis_info_field(out: str, key: str) -> str:
    prefix = f"{key}:"
    for line in (out or "").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


class DevRedisPlugin(ServicePlugin):
    service = "dev-redis"
    category = "cloud"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        if not container_exists_on_vm(cfg, vm_ip, "dev-redis", root):
            return [VerifyCheck("dev-redis", "ping", True, "dev profile not deployed (skipped)")]

        checks: list[VerifyCheck] = []
        dev_pass = secrets.get("DEV_REDIS_PASSWORD", "")
        if dev_pass:
            rc, out = docker_exec_on_vm(
                cfg,
                "dev-redis",
                ["redis-cli", "ping"],
                vm_ip,
                root,
                timeout=15,
                secret_environment={"REDISCLI_AUTH": dev_pass},
            )
        else:
            rc, out = docker_exec_on_vm(cfg, "dev-redis", ["redis-cli", "ping"], vm_ip, root, timeout=15)
        ping_ok = rc == 0 and "PONG" in (out or "").upper()
        checks.append(VerifyCheck("dev-redis", "ping", ping_ok, "PONG" if ping_ok else (out or "PING failed")[:120]))
        if not ping_ok or not dev_pass:
            return checks

        rc, out = docker_exec_on_vm(
            cfg,
            "dev-redis",
            ["redis-cli", "INFO", "memory"],
            vm_ip,
            root,
            timeout=15,
            secret_environment={"REDISCLI_AUTH": dev_pass},
        )
        if rc == 0:
            maxmem = _parse_redis_info_field(out, "maxmemory")
            used = _parse_redis_info_field(out, "used_memory")
            try:
                max_i = int(maxmem or "0")
                used_i = int(used or "0")
            except ValueError:
                max_i, used_i = 0, 0
            if max_i > 0:
                mem_ok = used_i < max_i
                checks.append(
                    VerifyCheck(
                        "dev-redis",
                        "memory",
                        mem_ok,
                        f"used {used_i} < max {max_i}" if mem_ok else f"used {used_i} >= max {max_i}",
                    )
                )
            else:
                checks.append(VerifyCheck("dev-redis", "memory", True, "no maxmemory cap configured"))

        rc, out = docker_exec_on_vm(
            cfg,
            "dev-redis",
            ["redis-cli", "ACL", "LIST"],
            vm_ip,
            root,
            timeout=15,
            secret_environment={"REDISCLI_AUTH": dev_pass},
        )
        if rc == 0:
            nopass = any("nopass" in ln.lower() for ln in (out or "").splitlines())
            checks.append(
                VerifyCheck(
                    "dev-redis",
                    "acl_auth",
                    not nopass,
                    "default user requires auth" if not nopass else "nopass user present",
                )
            )

        return checks
