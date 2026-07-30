"""redis service plugin.

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
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


def _parse_redis_info_field(out: str, key: str) -> str:
    prefix = f"{key}:"
    for line in (out or "").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


class RedisPlugin(ServicePlugin):
    service = "redis"
    category = "management"

    def before_runtime_start(self, context, services: tuple[str, ...]) -> tuple[str, ...]:
        """Reconcile a rotated Redis password before the compose wave.

        Redis keeps its data volume and does not reload ``requirepass`` when the
        bind-mounted config changes.  Probe the desired credential first; when
        it is rejected, migrate the running instance using the old password
        read from its mounted config.  A narrowly scoped force-recreate is the
        final fallback and is verified before startup continues.
        """
        if self.service not in services:
            return services
        desired = context.environment("REDIS_PASSWORD")
        if not desired:
            return services

        inspect = context.run_host(["docker", "inspect", "redis"])
        if getattr(inspect, "returncode", 1) != 0:
            context.log("Redis: container absent; password reconciliation skipped")
            return services

        from toolkit.core.ops.automation import docker_exec

        def probe(password: str) -> bool:
            rc, output = docker_exec(
                "redis",
                ["redis-cli", "ping"],
                timeout=15,
                secret_environment={"REDISCLI_AUTH": password},
            )
            # redis-cli exits zero even when AUTH fails, so require the
            # protocol response rather than trusting the process status.
            return rc == 0 and "PONG" in (output or "").upper()

        if probe(desired):
            context.log("Redis: existing password accepted")
            return services

        # Read the old value only inside the container and feed the new value
        # to redis-cli over stdin. Neither credential enters process argv or
        # crosses back through captured command output.
        migrate_cmd = [
            "sh",
            "-ec",
            "old=$(awk '$1 == \"requirepass\" {print $2; exit}' /run/redis/redis.conf); "
            'test -n "$old"; export REDISCLI_AUTH="$old"; '
            'printf %s "$REDIS_NEW_PASSWORD" | redis-cli --no-auth-warning -x CONFIG SET requirepass',
        ]
        migrate_rc, migrate_output = docker_exec(
            "redis",
            migrate_cmd,
            timeout=20,
            secret_environment={"REDIS_NEW_PASSWORD": desired},
        )
        if migrate_rc == 0 and "OK" in (migrate_output or "").upper() and probe(desired):
            context.log("Redis: password reconciled in place")
            return services

        context.warn("Redis: in-place password migration failed; recreating only Redis")
        recreated = context.compose("up", "-d", "--force-recreate", "--no-deps", "redis")
        if getattr(recreated, "returncode", 1) != 0:
            raise RuntimeError("Redis password reconciliation failed during force-recreate")
        if not context.wait_until_healthy("redis", ("redis",)) or not probe(desired):
            raise RuntimeError("Redis password reconciliation failed after force-recreate")
        context.log("Redis: password reconciled after force-recreate")
        return services

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        password = context.secrets.get("REDIS_PASSWORD", "")
        context.write_text(
            "generated/redis.conf",
            f"maxmemory 512mb\nmaxmemory-policy allkeys-lru\nrequirepass {password}\n",
        )
        context.write_text(
            "generated/redis-healthcheck.sh",
            (
                "#!/bin/sh\n"
                "PASS=$(awk '/^requirepass/{print $2}' /run/redis/redis.conf)\n"
                "printf 'AUTH %s\\nPING\\n' \"$PASS\" | redis-cli | grep -q PONG\n"
            ),
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Direct redis-cli PING with AUTH — exporter metrics alone can miss auth misconfig."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        checks: list[VerifyCheck] = []
        if cfg.domain == "localhost":
            return [VerifyCheck("redis", "ping", True, "skipped (localhost)")]
        if not cfg.category_enabled("management"):
            return [VerifyCheck("redis", "ping", True, "management disabled (skipped)")]
        if not container_exists_on_vm(cfg, vm_ip, "redis", root):
            return [VerifyCheck("redis", "ping", False, "container missing")]

        redis_pass = secrets.get("REDIS_PASSWORD", "")
        if redis_pass:
            rc, out = docker_exec_on_vm(
                cfg,
                "redis",
                ["redis-cli", "ping"],
                vm_ip,
                root,
                timeout=15,
                secret_environment={"REDISCLI_AUTH": redis_pass},
            )
        else:
            rc, out = docker_exec_on_vm(
                cfg, "redis", ["sh", "/usr/local/bin/redis-healthcheck.sh"], vm_ip, root, timeout=15
            )
        ping_ok = rc == 0 and "PONG" in (out or "").upper()
        checks.append(
            VerifyCheck("redis", "ping", ping_ok, "PONG via AUTH" if ping_ok else (out or "PING failed")[:120])
        )
        if not ping_ok or not redis_pass:
            return checks

        rc, out = docker_exec_on_vm(
            cfg,
            "redis",
            ["redis-cli", "INFO", "memory"],
            vm_ip,
            root,
            timeout=15,
            secret_environment={"REDISCLI_AUTH": redis_pass},
        )
        if rc == 0:
            maxmem = _parse_redis_info_field(out, "maxmemory")
            used = _parse_redis_info_field(out, "used_memory")
            policy = _parse_redis_info_field(out, "maxmemory_policy")
            try:
                max_i = int(maxmem or "0")
                used_i = int(used or "0")
            except ValueError:
                max_i, used_i = 0, 0
            if max_i > 0:
                mem_ok = used_i < max_i
                detail = f"used {used_i} < max {max_i}, policy={policy or 'unknown'}"
                checks.append(VerifyCheck("redis", "memory", mem_ok, detail))
            else:
                checks.append(VerifyCheck("redis", "memory", True, "no maxmemory cap configured"))

        rc, out = docker_exec_on_vm(
            cfg,
            "redis",
            ["redis-cli", "ACL", "LIST"],
            vm_ip,
            root,
            timeout=15,
            secret_environment={"REDISCLI_AUTH": redis_pass},
        )
        if rc == 0:
            acl_lines = [ln for ln in (out or "").splitlines() if ln.strip()]
            nopass = any("nopass" in ln.lower() for ln in acl_lines)
            checks.append(
                VerifyCheck(
                    "redis",
                    "acl_auth",
                    not nopass,
                    "default user requires auth" if not nopass else "nopass user present",
                )
            )

        rc, out = docker_exec_on_vm(
            cfg,
            "redis",
            ["redis-cli", "INFO", "keyspace"],
            vm_ip,
            root,
            timeout=15,
            secret_environment={"REDISCLI_AUTH": redis_pass},
        )
        if rc == 0:
            keyspace = (out or "").strip()
            checks.append(
                VerifyCheck(
                    "redis",
                    "authelia_keyspace",
                    True,
                    keyspace[:80] if keyspace else "no keys yet (session store reachable)",
                )
            )

        return checks
