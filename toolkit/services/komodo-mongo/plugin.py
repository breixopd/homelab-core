"""komodo-mongo service plugin — defaults from service.yaml; override post_start/verify/heal when needed."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class KomodoMongoPlugin(ServicePlugin):
    service = "komodo-mongo"
    category = "management"

    _MONGO_URI = "mongodb://127.0.0.1:27017/?directConnection=true&serverSelectionTimeoutMS=3000&connectTimeoutMS=3000"
    _RECOVERY_NAME = "komodo-mongo-password-recovery"
    _IMMUTABLE_IMAGE = re.compile(r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")

    def before_runtime_start(self, context, services: tuple[str, ...]) -> tuple[str, ...]:
        """Reconcile Mongo's root password before Compose can recreate it.

        Mongo only applies ``MONGO_INITDB_ROOT_PASSWORD`` on first
        initialization.  A generated-secret rotation therefore needs an
        explicit service-owned migration before the normal startup wave.
        """
        if self.service not in services or not context.environment("KOMODO_DATABASE_PASSWORD"):
            return services
        inspect = context.run_host(["docker", "inspect", "komodo-mongo"])
        if getattr(inspect, "returncode", 1) != 0:
            context.log("Komodo Mongo: container absent; password reconciliation skipped")
            return services

        password = context.environment("KOMODO_DATABASE_PASSWORD")
        username = context.environment("KOMODO_DATABASE_USERNAME", "komodo")
        _validate_credential(username, password)
        from toolkit.core.ops.automation import docker_exec

        auth_cmd = _mongosh_command(
            self._MONGO_URI,
            '-u "$MONGO_USERNAME" -p "$MONGO_NEW_PASSWORD"',
            "db.adminCommand('ping')",
        )
        rc, _ = docker_exec(
            "komodo-mongo",
            auth_cmd,
            timeout=20,
            secret_environment={"MONGO_USERNAME": username, "MONGO_NEW_PASSWORD": password},
        )
        if rc == 0:
            context.log("Komodo Mongo: existing root credentials accepted")
            return services

        # Stop the dependent application while the database credential is
        # migrated.  Failure is handled by the bounded recovery path below.
        context.run_host(["docker", "stop", "komodo-core"])
        change_cmd = _mongosh_command(
            self._MONGO_URI,
            '-u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD"',
            "db.getSiblingDB('admin').changeUserPassword(process.env.MONGO_USERNAME, process.env.MONGO_NEW_PASSWORD)",
        )
        rc, _ = docker_exec(
            "komodo-mongo",
            change_cmd,
            timeout=25,
            secret_environment={"MONGO_USERNAME": username, "MONGO_NEW_PASSWORD": password},
        )
        if rc == 0:
            context.log("Komodo Mongo: root password reconciled through authenticated runtime")
            return services

        context.warn("Komodo Mongo: authenticated password migration failed; starting isolated recovery")
        self._recover_without_auth(context, username, password)
        return services

    def _recover_without_auth(self, context, username: str, password: str) -> None:
        source_text = context.environment("KOMODO_MONGO_DATA_SOURCE")
        source = _validate_data_source(source_text)
        try:
            image = self.compose_service()["image"]
        except RuntimeError:
            import yaml

            compose = yaml.safe_load((Path(__file__).parent / "compose.yaml").read_text(encoding="utf-8"))
            image = compose["services"][self.service]["image"]
        if not isinstance(image, str) or not self._IMMUTABLE_IMAGE.fullmatch(image):
            raise RuntimeError("Komodo Mongo recovery requires an immutable image reference")

        # The recovery process is deliberately isolated from every network and
        # is bounded by the mongosh selection timeout plus a short retry loop.
        context.run_host(["docker", "stop", "komodo-mongo"])
        context.run_host(["docker", "rm", "-f", self._RECOVERY_NAME])
        run = context.run_host(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self._RECOVERY_NAME,
                "--network",
                "none",
                "--mount",
                f"type=bind,src={source},dst=/data/db",
                image,
                "--dbpath",
                "/data/db",
                "--bind_ip",
                "127.0.0.1",
                "--port",
                "27018",
                "--noauth",
            ]
        )
        if getattr(run, "returncode", 1) != 0:
            raise RuntimeError("Komodo Mongo isolated recovery container failed to start")
        try:
            from toolkit.core.ops.automation import docker_exec

            uri = self._MONGO_URI.replace("27017", "27018")
            command = _mongosh_command(
                uri,
                "",
                "db.getSiblingDB('admin').changeUserPassword("
                "process.env.MONGO_USERNAME, process.env.MONGO_NEW_PASSWORD)",
            )
            last_rc = 1
            for _ in range(12):
                last_rc, _ = docker_exec(
                    self._RECOVERY_NAME,
                    command,
                    timeout=8,
                    secret_environment={"MONGO_USERNAME": username, "MONGO_NEW_PASSWORD": password},
                )
                if last_rc == 0:
                    context.log("Komodo Mongo: isolated password recovery completed")
                    return
                time.sleep(1)
            raise RuntimeError(f"Komodo Mongo isolated recovery did not converge (exit {last_rc})")
        finally:
            context.run_host(["docker", "rm", "-f", self._RECOVERY_NAME])

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        if not cfg.category_enabled("management"):
            return [VerifyCheck("komodo-mongo", "ping", True, "management not enabled")]

        if cfg.domain == "localhost":
            return [VerifyCheck("komodo-mongo", "ping", True, "skipped (localhost)")]

        if not container_exists_on_vm(cfg, vm_ip, "komodo-mongo", root):
            return [VerifyCheck("komodo-mongo", "ping", False, "container missing")]

        checks: list[VerifyCheck] = []
        # Probe with the exact credentials injected into the running container.
        # Re-delivering the controller secret through a wrapper is redundant and
        # made verification diverge from the runtime that Komodo actually uses.
        mongo_uri = (
            "mongodb://127.0.0.1:27017/?directConnection=true&serverSelectionTimeoutMS=3000&connectTimeoutMS=3000"
        )
        ping_cmd = [
            "sh",
            "-c",
            f"timeout 15s mongosh '{mongo_uri}' --norc --quiet "
            '-u "$MONGO_INITDB_ROOT_USERNAME" '
            '-p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin '
            "--eval \"JSON.stringify(db.adminCommand('ping'))\"",
        ]
        rc, out = docker_exec_on_vm(cfg, "komodo-mongo", ping_cmd, vm_ip, root, timeout=30)
        ok = rc == 0 and "ok" in (out or "").lower()
        checks.append(VerifyCheck("komodo-mongo", "ping", ok, (out or "ping failed")[:120]))

        ready_eval = (
            f"timeout 15s mongosh '{mongo_uri}' --norc --quiet "
            '-u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
            '--authenticationDatabase admin --eval "JSON.stringify(db.adminCommand({hello:1}).ok)"'
        )
        rc2, out2 = 1, ""
        for attempt in range(2):
            rc2, out2 = docker_exec_on_vm(
                cfg,
                "komodo-mongo",
                ["sh", "-c", ready_eval],
                vm_ip,
                root,
                timeout=30,
            )
            if rc2 == 0 and "1" in (out2 or ""):
                break
            if attempt == 0:
                # MongoDB can accept ping just before it is ready to select.
                time.sleep(1)
        ready = rc2 == 0 and "1" in (out2 or "")
        checks.append(
            VerifyCheck(
                "komodo-mongo",
                "ready",
                ready,
                "server ready" if ready else (out2 or "server readiness check failed")[:120],
            )
        )
        return checks


def _validate_credential(username: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("invalid Komodo Mongo username")
    if not password or any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("invalid Komodo Mongo password")


def _validate_data_source(value: str) -> Path:
    if not value:
        raise RuntimeError("Komodo Mongo data source is not configured")
    source = Path(value)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise RuntimeError("Komodo Mongo data source must be an existing local directory")
    return source.resolve()


def _mongosh_command(uri: str, credentials: str, expression: str) -> list[str]:
    auth = f" {credentials}" if credentials else ""
    return [
        "sh",
        "-ec",
        f"exec mongosh '{uri}' --norc --quiet{auth} --authenticationDatabase admin --eval \"{expression}\"",
    ]
