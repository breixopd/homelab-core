"""PostgreSQL-owned pre-deploy dump and restore operations."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from toolkit.core.config.roles import uses_remote_nodes
from toolkit.core.ops.dump_repository import DumpRecord, DumpRepository

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


MAX_DUMPS = 7
_MAX_VALIDATED_DUMP_BYTES = 64 * 1024 * 1024
_DUMP_DIRECTORY = "/opt/homelab/generated/pre-deploy-dumps"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class _PostgresContract:
    service: str
    container: str
    admin_user: str
    admin_database: str


def _contract(service: str) -> _PostgresContract:
    from toolkit.core.manifest.catalog import load_service_catalog

    manifest = load_service_catalog().require(service)
    provider = manifest.database_provider
    endpoint = manifest.service_endpoint
    if provider is None or provider.engine != "postgresql" or endpoint is None:
        raise RuntimeError(f"{service!r} is not a valid PostgreSQL database provider")
    admin_user = manifest.variables.get(provider.admin_username_env, "")
    admin_database = manifest.variables.get(provider.admin_database_env, "")
    if not _IDENTIFIER.fullmatch(admin_user) or not _IDENTIFIER.fullmatch(admin_database):
        raise RuntimeError(f"{service!r} must declare safe PostgreSQL administrator identifiers")
    return _PostgresContract(
        service=service,
        container=endpoint.compose_service or service,
        admin_user=admin_user,
        admin_database=admin_database,
    )


def _node(cfg: Config, service: str, override: str | None) -> str:
    if override is not None:
        cfg.node_ip(override)
        return override
    from toolkit.core.manifest.placement import service_node

    return service_node(cfg, service)


def pre_deploy_dump(cfg: Config, root: Path, *, service: str, vm: str | None = None) -> str | None:
    """Run a PostgreSQL ``pg_dumpall`` and retain the latest bounded history."""
    contract = _contract(service)
    if not uses_remote_nodes(cfg):
        return _local_dump(root, contract)
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    node = _node(cfg, service, vm)
    remote_path = f"{_DUMP_DIRECTORY}/pre-deploy-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
    remote_dir = shlex.quote(_DUMP_DIRECTORY)
    script = (
        "set -euo pipefail; "
        f"umask 077 && install -d -m 0700 {remote_dir} && "
        f"docker exec {shlex.quote(contract.container)} pg_dumpall -U {shlex.quote(contract.admin_user)} "
        f"-d {shlex.quote(contract.admin_database)} 2>/dev/null | gzip > {shlex.quote(remote_path)} && "
        f"test -s {shlex.quote(remote_path)} && gzip -t {shlex.quote(remote_path)} && "
        f'test "$(gzip -cd {shlex.quote(remote_path)} | wc -c)" -gt 0 && '
        f"ls -lh {shlex.quote(remote_path)} && "
        f"(cd {remote_dir} && ls -t pre-deploy-*.sql.gz 2>/dev/null | tail -n +{MAX_DUMPS + 1} | xargs -r rm -f) && "
        "echo OK"
    )
    command = f"bash -o pipefail -c {shlex.quote(script)}"
    try:
        rc, output, _error = ssh_run_on_vm(cfg, cfg.node_ip(node), command, root=root, timeout=120)
    except Exception:
        return None
    if rc != 0 or "OK" not in (output or ""):
        return None
    return remote_path


def _local_dump(root: Path, contract: _PostgresContract) -> str | None:
    dump_dir = root / "generated" / "pre-deploy-dumps"
    dump_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dump_dir.chmod(0o700)
    path = dump_dir / f"pre-deploy-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as output:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    contract.container,
                    "pg_dumpall",
                    "-U",
                    contract.admin_user,
                    "-d",
                    contract.admin_database,
                ],
                stdout=cast(IO[bytes], output),
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            return None
    except (OSError, subprocess.SubprocessError):
        path.unlink(missing_ok=True)
        return None
    if not _valid_dump_file(path):
        path.unlink(missing_ok=True)
        return None
    old = sorted(dump_dir.glob("pre-deploy-*.sql.gz"), key=lambda item: item.stat().st_mtime)
    for old_dump in old[:-MAX_DUMPS]:
        old_dump.unlink(missing_ok=True)
    return str(path)


def _valid_dump_file(path: Path) -> bool:
    """Require a regular, non-empty gzip containing non-empty SQL output."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            return False
        total = 0
        with gzip.open(path, "rb") as source:
            while chunk := source.read(64 * 1024):
                total += len(chunk)
                if total > _MAX_VALIDATED_DUMP_BYTES:
                    return False
        return total > 0
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def restore_dump(
    cfg: Config,
    root: Path,
    record: DumpRecord,
    *,
    service: str,
    vm: str | None = None,
) -> bool:
    """Restore a checksum-verified PostgreSQL dump selected by the framework."""
    contract = _contract(service)
    if not uses_remote_nodes(cfg):
        return _local_restore(record, contract)
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    node = _node(cfg, service, vm)
    path = shlex.quote(record.path)
    digest = shlex.quote(record.sha256)
    pipeline = (
        f"test \"$(sha256sum {path} | cut -d ' ' -f1)\" = {digest} && "
        f"gunzip -c {path} | docker exec -i {shlex.quote(contract.container)} psql "
        f"-v ON_ERROR_STOP=1 -U {shlex.quote(contract.admin_user)} -d {shlex.quote(contract.admin_database)}"
    )
    command = f"bash -o pipefail -c {shlex.quote(pipeline)}"
    try:
        rc, _output, _error = ssh_run_on_vm(cfg, cfg.node_ip(node), command, root=root, timeout=300)
    except Exception:
        return False
    return rc == 0


def _local_restore(record: DumpRecord, contract: _PostgresContract) -> bool:
    try:
        path = Path(record.path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.sha256:
            return False
        with gzip.open(path, "rb") as source:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    contract.container,
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    contract.admin_user,
                    "-d",
                    contract.admin_database,
                ],
                stdin=cast(IO[bytes], source),
                capture_output=True,
                timeout=300,
                check=False,
            )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def list_dumps(cfg: Config, root: Path, *, service: str, vm: str | None = None) -> list[DumpRecord]:
    """List validated PostgreSQL pre-deploy dumps from the owning node."""
    if not uses_remote_nodes(cfg):
        return DumpRepository.local(root / "generated" / "pre-deploy-dumps").list()
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    node = _node(cfg, service, vm)
    command = (
        f"for f in {shlex.quote(_DUMP_DIRECTORY)}/pre-deploy-*.sql.gz; do "
        '[ -f "$f" ] || continue; '
        'printf \'%s\\t%s\\t%s\\n\' "$f" "$(stat -c %s "$f")" '
        '"$(sha256sum "$f" | cut -d \' \' -f1)"; done'
    )
    try:
        rc, output, _error = ssh_run_on_vm(cfg, cfg.node_ip(node), command, root=root, timeout=20)
    except Exception:
        return []
    if rc != 0 or not output:
        return []
    entries: list[dict[str, object]] = []
    for line in output.strip().splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        path, size_bytes, digest = fields
        entries.append({"path": path, "size_bytes": size_bytes, "sha256": digest})
    return DumpRepository.remote(_DUMP_DIRECTORY, entries).list()


def run_restore_drill(
    cfg: Config,
    root: Path,
    record: DumpRecord,
    *,
    service: str,
    vm: str | None = None,
) -> tuple[bool, int, str]:
    """Restore one dump in an isolated PostgreSQL container and count databases."""
    contract = _contract(service)
    if uses_remote_nodes(cfg):
        node = _node(cfg, service, vm)
        return _remote_restore_drill(cfg, root, record, contract, vm=node)
    return _local_restore_drill(record, contract)


def _container_name() -> str:
    return f"homelab-restore-drill-{uuid.uuid4().hex[:12]}"


def _remote_restore_drill(
    cfg: Config,
    root: Path,
    record: DumpRecord,
    contract: _PostgresContract,
    *,
    vm: str,
) -> tuple[bool, int, str]:
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    name = shlex.quote(_container_name())
    path = shlex.quote(record.path)
    digest = shlex.quote(record.sha256)
    script = f"""set -euo pipefail
cleanup() {{ docker rm -f {name} >/dev/null 2>&1 || true; }}
trap cleanup EXIT
image="$(docker inspect --format '{{{{.Image}}}}' {shlex.quote(contract.container)})"
test -n "$image"
docker run -d --name {name} \\
  -e POSTGRES_HOST_AUTH_METHOD=trust \\
  -e POSTGRES_USER=restore_admin \\
  -e POSTGRES_DB=restore_control \\
  "$image" >/dev/null
ready=0
for _ in $(seq 1 60); do
  if test "$(docker exec {name} psql -U restore_admin -d restore_control -Atqc 'SELECT 1' 2>/dev/null)" = 1; then
    ready=1
    break
  fi
  sleep 1
done
if test "$ready" != 1; then echo 'DRILL_ERROR=target database readiness timed out' >&2; exit 1; fi
if test "$(sha256sum {path} | cut -d ' ' -f1)" != {digest}; then
  echo 'DRILL_ERROR=dump checksum mismatch' >&2
  exit 1
fi
if ! gunzip -c {path} | \\
  docker exec -i {name} psql -v ON_ERROR_STOP=1 -U restore_admin -d restore_control >/dev/null; then
  echo 'DRILL_ERROR=database restore command failed' >&2
  exit 1
fi
count="$(docker exec {name} psql -U restore_admin -d postgres \\
  -Atqc 'SELECT count(*) FROM pg_database WHERE datallowconn')"
case "$count" in (*[!0-9]*|'') echo 'DRILL_ERROR=database verification query failed' >&2; exit 1;; esac
printf 'DATABASE_COUNT=%s\\n' "$count"
"""
    command = f"bash -o pipefail -c {shlex.quote(script)}"
    try:
        rc, output, error = ssh_run_on_vm(cfg, cfg.node_ip(vm), command, root=root, timeout=900)
    except Exception:
        return False, 0, "restore drill process execution failed"
    match = re.search(r"^DATABASE_COUNT=(\d+)$", output or "", re.MULTILINE)
    detail = re.search(r"^DRILL_ERROR=([A-Za-z0-9 ._:-]{1,200})$", f"{output}\n{error}", re.MULTILINE)
    return (
        rc == 0 and match is not None,
        int(match.group(1)) if match else 0,
        detail.group(1) if detail else "isolated database restore or verification query failed",
    )


def _run(command: list[str], *, timeout: int, stdin: IO[bytes] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, stdin=stdin, capture_output=stdin is None, timeout=timeout, check=False)


def _local_restore_drill(record: DumpRecord, contract: _PostgresContract) -> tuple[bool, int, str]:
    name = _container_name()
    try:
        digest = hashlib.sha256()
        with Path(record.path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record.sha256:
            return False, 0, "dump checksum mismatch"
        inspect = _run(["docker", "inspect", "--format", "{{.Image}}", contract.container], timeout=20)
        image = inspect.stdout.decode(errors="replace").strip() if inspect.returncode == 0 else ""
        if not image:
            return False, 0, "PostgreSQL image is unavailable"
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_USER=restore_admin",
                "-e",
                "POSTGRES_DB=restore_control",
                image,
            ],
            timeout=120,
        )
        if started.returncode != 0:
            return False, 0, "isolated PostgreSQL container failed to start"
        for _ in range(60):
            ready = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "psql",
                    "-U",
                    "restore_admin",
                    "-d",
                    "restore_control",
                    "-Atqc",
                    "SELECT 1",
                ],
                timeout=10,
            )
            if ready.returncode == 0 and ready.stdout.decode(errors="replace").strip() == "1":
                break
            time.sleep(1)
        else:
            return False, 0, "target database readiness timed out"
        with gzip.open(record.path, "rb") as stream:
            restored = _run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    "restore_admin",
                    "-d",
                    "restore_control",
                ],
                stdin=cast(IO[bytes], stream),
                timeout=900,
            )
        if restored.returncode != 0:
            return False, 0, "database restore command failed"
        query = _run(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "restore_admin",
                "-d",
                "postgres",
                "-Atqc",
                "SELECT count(*) FROM pg_database WHERE datallowconn",
            ],
            timeout=30,
        )
        output = query.stdout.decode(errors="replace").strip() if query.returncode == 0 else ""
        if output.isdigit():
            return True, int(output), ""
        return False, 0, "database verification query failed"
    except (OSError, subprocess.SubprocessError):
        return False, 0, "restore drill process execution failed"
    finally:
        try:
            _run(["docker", "rm", "-f", name], timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
