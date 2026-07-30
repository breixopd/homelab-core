"""Non-destructive Wazuh indexer credential reconciliation helpers."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, secrets_path
from toolkit.core.ops.automation import docker_curl
from toolkit.services.sdk.http import basic_auth_header

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

_MAX_RUNTIME_RESPONSE_BYTES = 65_536


def _indexer_auth_ok(root: Path, *, docker_bin: str = "docker") -> bool:
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    password = secrets.get("WAZUH_INDEXER_PASSWORD", "")
    if not password:
        return False
    rc, output = docker_curl(
        "wazuh-indexer",
        "https://localhost:9200/_cluster/health",
        headers={"Authorization": basic_auth_header("admin", password)},
        insecure_tls=True,
        docker_bin=docker_bin,
    )
    if rc != 0:
        return False
    return '"status":"green"' in output or '"status":"yellow"' in output


def _run_securityadmin(*, docker_bin: str = "docker", attempts: int = 6) -> str:
    """Apply OpenSearch security configuration, including mounted user credentials."""
    cmd = (
        "export OPENSEARCH_JAVA_HOME=/usr/share/wazuh-indexer/jdk && "
        "/usr/share/wazuh-indexer/plugins/opensearch-security/tools/securityadmin.sh "
        "-cd /usr/share/wazuh-indexer/config/opensearch-security/ "
        "-icl -nhnv "
        "-cacert /usr/share/wazuh-indexer/config/certs/root-ca.pem "
        "-cert /usr/share/wazuh-indexer/config/certs/admin.pem "
        "-key /usr/share/wazuh-indexer/config/certs/admin-key.pem"
    )
    last_out = ""
    for attempt in range(attempts):
        proc = subprocess.run(
            [docker_bin, "exec", "wazuh-indexer", "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        last_out = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0 and "Done with success" in last_out:
            return "Wazuh: securityadmin applied internal_users"
        if attempt + 1 < attempts:
            time.sleep(10)
    return f"Wazuh: securityadmin failed ({last_out[:160]})"


def _wait_indexer_http(*, docker_bin: str = "docker", attempts: int = 40) -> bool:
    for _ in range(attempts):
        proc = subprocess.run(
            [
                docker_bin,
                "exec",
                "wazuh-indexer",
                "curl",
                "-sk",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "https://localhost:9200",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.stdout.strip() in ("401", "503", "200"):
            return True
        time.sleep(5)
    return False


def ensure_wazuh_indexer_healthy(root: Path | None = None, *, docker_bin: str = "docker") -> list[str]:
    """Reconcile Wazuh credentials without mutating persisted index data."""
    root = Path(root or DEFAULT_HOMELAB_ROOT)
    proc = subprocess.run(
        [docker_bin, "inspect", "--format", "{{.State.Status}}", "wazuh-indexer"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    status = proc.stdout.strip()
    if status not in ("running", "restarting", "created", "exited"):
        return [f"Wazuh: wazuh-indexer state {status!r} - skip heal"]
    if status != "running":
        return [f"Wazuh: wazuh-indexer is {status}; persisted index data preserved; operator repair required"]

    health = subprocess.run(
        [docker_bin, "inspect", "--format", "{{.State.Health.Status}}", "wazuh-indexer"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if health.stdout.strip() == "healthy" or _indexer_auth_ok(root, docker_bin=docker_bin):
        return ["Wazuh: wazuh-indexer OK"]
    if not _wait_indexer_http(docker_bin=docker_bin):
        return ["Wazuh: indexer HTTP unavailable; persisted index data preserved; operator repair required"]

    security_log = _run_securityadmin(docker_bin=docker_bin)
    if _indexer_auth_ok(root, docker_bin=docker_bin):
        return [security_log, "Wazuh: credentials reconciled; indexer state preserved"]
    return [
        security_log,
        "Wazuh: credential reconciliation failed; persisted index data preserved; operator repair required",
    ]


def _reconciliation_succeeded(logs: list[str]) -> bool:
    return logs[-1:] in (
        ["Wazuh: wazuh-indexer OK"],
        ["Wazuh: credentials reconciled; indexer state preserved"],
    )


def _runtime_reconcile_response(root: Path) -> dict[str, object]:
    logs = ensure_wazuh_indexer_healthy(root)
    return {"ok": _reconciliation_succeeded(logs), "logs": logs}


def reconcile_wazuh_security(cfg: Config, root: Path) -> list[str]:
    """Run bounded Wazuh security reconciliation on the indexer's owning node."""
    from toolkit.core.ansible.ansible_ssh import sanitize_probe_output, ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    command = (
        f"cd {DEFAULT_HOMELAB_ROOT} && "
        f"{DEFAULT_HOMELAB_ROOT}/.venv/bin/python3 -m toolkit.services.wazuh-indexer.bootstrap "
        "--runtime-reconcile"
    )
    rc, out, err = ssh_run_on_vm(
        cfg,
        service_address(cfg, "wazuh-indexer"),
        command,
        root=root,
        timeout=300,
        retries=2,
    )
    if rc != 0:
        detail = sanitize_probe_output(err, max_len=100) or f"exit {rc}"
        raise RuntimeError(f"Wazuh security reconciliation transport failed ({detail})")
    if len(out.encode("utf-8")) > _MAX_RUNTIME_RESPONSE_BYTES:
        raise RuntimeError("Wazuh security reconciliation response was too large")
    try:
        response = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Wazuh security reconciliation returned invalid JSON") from exc
    if not isinstance(response, dict) or set(response) != {"ok", "logs"}:
        raise RuntimeError("Wazuh security reconciliation returned an invalid response")
    ok = response["ok"]
    logs = response["logs"]
    if (
        not isinstance(ok, bool)
        or not isinstance(logs, list)
        or not logs
        or len(logs) > 20
        or not all(isinstance(line, str) and line.strip() and "\n" not in line and len(line) <= 500 for line in logs)
    ):
        raise RuntimeError("Wazuh security reconciliation returned an invalid response")
    if not ok:
        raise RuntimeError("Wazuh security reconciliation did not converge")
    return logs


def _runtime_reconcile_cli() -> int:
    response = _runtime_reconcile_response(Path(DEFAULT_HOMELAB_ROOT))
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-reconcile", action="store_true")
    args = parser.parse_args()
    if not args.runtime_reconcile:
        parser.error("--runtime-reconcile is required")
    raise SystemExit(_runtime_reconcile_cli())
