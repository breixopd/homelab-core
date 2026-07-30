"""SeaweedFS post-deploy bootstrap: ensure expected S3 buckets exist on the filer."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
# Buckets every cloud-enabled deploy expects. Nextcloud and Immich use S3
# storage backends; "backups" is the target for Kopia/external backup tooling.
SEAWEEDFS_EXPECTED_BUCKETS: tuple[str, ...] = ("nextcloud", "immich", "backups")
_READINESS_ATTEMPTS = 6
_READINESS_DELAY_SECONDS = 5.0


def _weed_shell(secrets: dict[str, str], command: str) -> tuple[int, str]:
    """Run a single ``weed shell`` command inside the seaweedfs container.

    Pipes the command on stdin so we don't depend on a TTY. The all-in-one
    server image runs master+volume+filer+S3 in one process, so ``weed shell``
    finds the local filer without extra flags.
    """

    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", "seaweedfs", "weed", "shell"],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def bootstrap_seaweedfs_buckets(
    config: Config,
    secrets: dict[str, str],
    *,
    buckets: tuple[str, ...] | None = None,
) -> list[str]:
    """Idempotently create the expected S3 buckets on the SeaweedFS filer.

    Runs only when ``services.cloud`` is on. Uses ``weed shell`` inside the
    seaweedfs container — the all-in-one image exposes the filer on localhost
    so no ``-filer`` flag is needed. Each bucket is created with
    ``s3.bucket.create`` (no-op if it already exists). Buckets are owned by the
    ``admin`` S3 identity so they are reachable with the admin credentials in
    ``generated/seaweedfs-s3.json``.
    """
    logs: list[str] = []
    if not config.category_enabled("cloud"):
        return logs
    expected = tuple(buckets) if buckets is not None else SEAWEEDFS_EXPECTED_BUCKETS

    # The healthcheck only proves that the filer HTTP listener is alive.  The
    # S3 control plane may still be wiring up, so retry the actual bucket API
    # before giving up.  This is deliberately bounded to keep post-start
    # hooks from hanging forever on a broken deployment.
    list_rc, list_out = _weed_shell(secrets, "s3.bucket.list")
    for attempt in range(1, _READINESS_ATTEMPTS):
        if list_rc == 0:
            break
        time.sleep(_READINESS_DELAY_SECONDS)
        list_rc, list_out = _weed_shell(secrets, "s3.bucket.list")
    if list_rc != 0:
        logs.append(
            f"SeaweedFS: weed shell unreachable after {_READINESS_ATTEMPTS} attempts "
            f"({(list_out or '')[:120]}) — skip bucket bootstrap"
        )
        return logs

    existing = {
        ln.split("\t", 1)[0].strip()
        for ln in (list_out or "").splitlines()
        if ln.strip() and not ln.startswith("s3.bucket")
    }
    created: list[str] = []
    for name in expected:
        if name in existing:
            continue
        rc, out = _weed_shell(secrets, f"s3.bucket.create -name {name} -owner admin")
        for _attempt in range(1, _READINESS_ATTEMPTS):
            if rc == 0:
                break
            time.sleep(_READINESS_DELAY_SECONDS)
            rc, out = _weed_shell(secrets, f"s3.bucket.create -name {name} -owner admin")
        if rc == 0:
            created.append(name)
        else:
            logs.append(f"SeaweedFS: bucket {name} create failed ({(out or '')[:120]})")
    if created:
        logs.append(f"SeaweedFS: created buckets {', '.join(created)}")
    else:
        logs.append(f"SeaweedFS: buckets already present ({', '.join(sorted(existing)) or 'none'})")
    return logs
