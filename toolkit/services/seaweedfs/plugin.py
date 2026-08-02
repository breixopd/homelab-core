"""seaweedfs service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from service.yaml.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


_READINESS_ATTEMPTS = 3
_READINESS_DELAY_SECONDS = 2.0
_BUCKET_OUTPUT_LIMIT = 64 * 1024
_BUCKET_RC_MARKER = "__HOMELAB_BUCKET_RC__"
_RESOURCE_LIMIT = 100


def _docker_curl_ready(cfg: Config, vm_ip: str, url: str, root: Path) -> tuple[int, str]:
    """Probe an internal endpoint briefly while SeaweedFS finishes startup."""
    from toolkit.services.sdk import docker_curl

    rc, body = docker_curl(cfg, vm_ip, "seaweedfs", url, root=root)
    for _attempt in range(1, _READINESS_ATTEMPTS):
        if rc == 0:
            break
        time.sleep(_READINESS_DELAY_SECONDS)
        rc, body = docker_curl(cfg, vm_ip, "seaweedfs", url, root=root)
    return rc, body


def _check_seaweedfs_s3(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """SeaweedFS S3 API is reachable on the S3 gateway itself."""
    import httpx
    from toolkit.services.sdk import VerifyCheck

    if cfg.is_multi_node:
        for path in ("/status", "/healthz"):
            rc, body = _docker_curl_ready(cfg, vm_ip, f"http://localhost:8333{path}", root)
            if rc == 0:
                detail = f"S3 API ok (8333{path})"
                if (body or "").strip():
                    detail += f" ({len(body.strip())} bytes)"
                return VerifyCheck("seaweedfs", "s3_status", True, detail)
        return VerifyCheck("seaweedfs", "s3_status", False, "S3 status unreachable")
    try:
        resp = httpx.get("http://localhost:8333/status", timeout=10)
    except httpx.HTTPError:
        resp = None
    ok = bool(resp and resp.status_code == 200)
    detail = f"HTTP {resp.status_code}" if resp else "S3 status unreachable"
    return VerifyCheck("seaweedfs", "s3_status", ok, detail)


def _check_seaweedfs_cluster(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Master cluster status — leader must be present."""
    import json

    from toolkit.services.sdk import VerifyCheck, docker_curl

    rc, body = docker_curl(cfg, vm_ip, "seaweedfs", "http://localhost:9333/cluster/status", root=root)
    if rc != 0 or not body:
        return VerifyCheck("seaweedfs", "cluster_leader", False, "cluster/status unreachable")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return VerifyCheck("seaweedfs", "cluster_leader", False, "invalid cluster JSON")
    leader = data.get("Leader") or data.get("leader") or ""
    is_leader = data.get("IsLeader") or data.get("isLeader")
    ok = bool(leader) or is_leader is True
    detail = f"leader={leader or 'self'}" if ok else "no leader in cluster status"
    return VerifyCheck("seaweedfs", "cluster_leader", ok, detail)


def _check_seaweedfs_filer(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Filer HTTP endpoint on :8888."""
    from toolkit.services.sdk import VerifyCheck

    rc, body = _docker_curl_ready(cfg, vm_ip, "http://localhost:8888/", root)
    ok = rc == 0
    detail = "filer reachable" if ok else "filer unreachable"
    if ok and (body or "").strip():
        detail += f" ({len(body.strip())} bytes)"
    return VerifyCheck("seaweedfs", "filer", ok, detail)


def _check_seaweedfs_s3_auth(cfg: Config, vm_ip: str, root: Path, secrets: dict[str, str]) -> VerifyCheck:
    """S3 gateway requires auth — anonymous list must fail when credentials configured."""
    from toolkit.services.sdk import VerifyCheck, docker_curl

    if not cfg.category_enabled("cloud"):
        return VerifyCheck("seaweedfs", "s3_auth", True, "cloud not enabled")
    access = secrets.get("SEAWEEDFS_S3_ACCESS_KEY", "")
    secret = secrets.get("SEAWEEDFS_S3_SECRET_KEY", "")
    if not access or not secret:
        return VerifyCheck("seaweedfs", "s3_auth", False, "SEAWEEDFS_S3_ACCESS_KEY/SECRET_KEY not set")

    rc_anon, _ = docker_curl(cfg, vm_ip, "seaweedfs", "http://localhost:8333/", root=root)
    anon_blocked = rc_anon != 0
    from toolkit.services.sdk import docker_exec_on_vm

    # Credentials are delivered through the stdin-backed secret environment
    # wrapper.  Keeping the command static prevents them from appearing in
    # SSH/docker argv, shell history, or diagnostic command output.
    rc_auth, out = docker_exec_on_vm(
        cfg,
        "seaweedfs",
        [
            "/bin/sh",
            "-ec",
            'curl --fail --silent --show-error --aws-sigv4 "aws:amz:us-east-1:s3" '
            '--user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" http://127.0.0.1:8333/',
        ],
        vm_ip,
        root,
        timeout=15,
        secret_environment={
            "AWS_ACCESS_KEY_ID": access,
            "AWS_SECRET_ACCESS_KEY": secret,
        },
    )
    if rc_auth != 0:
        ok = False
        detail = (out or "authenticated S3 probe failed")[:120]
    elif not anon_blocked:
        ok = False
        detail = "authenticated S3 list succeeded but anonymous access was also allowed"
    else:
        ok = True
        detail = "authenticated S3 list ok; anonymous access blocked"
    return VerifyCheck("seaweedfs", "s3_auth", ok, detail)


def _check_seaweedfs_s3_host_exposure(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """Confirm only the declared ingress node can reach published edge ports.

    Caddy reaches SeaweedFS over the shared edge network for same-node
    deployments and over the explicitly bound ``PRIVATE_IP`` listeners for
    cross-node deployments.  Probe both public routes so the filer cannot
    accidentally be exposed more broadly than S3.
    """
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address, service_node
    from toolkit.services.sdk import VerifyCheck, ssh_on_vm

    if not cfg.category_enabled("cloud"):
        return VerifyCheck("seaweedfs", "s3_host_exposure", True, "cloud not enabled")
    shell = (
        "for port_path in 8333/status 8888/; do "
        'port="${port_path%%/*}"; '
        f'if curl -sf --max-time 3 "http://{vm_ip}:$port_path" >/dev/null 2>&1; '
        'then printf "%s=OPEN\\n" "$port"; else printf "%s=CLOSED\\n" "$port"; fi; '
        "done"
    )
    ingress_service = provider_service_name("ingress")
    ingress_node = service_node(cfg, ingress_service)
    ingress_ip = service_address(cfg, ingress_service)
    peer_node = next((node for node in cfg.enabled_nodes if node != ingress_node), None)
    if peer_node is None:
        return VerifyCheck("seaweedfs", "s3_host_exposure", True, "single ingress node")
    peer_rc, peer_out, _ = ssh_on_vm(cfg, cfg.node_ip(peer_node), shell, root=root, timeout=15)
    ingress_rc, ingress_out, _ = ssh_on_vm(cfg, ingress_ip, shell, root=root, timeout=15)
    peer_states = dict(
        line.split("=", 1)
        for line in (peer_out or "").splitlines()
        if "=" in line and line.split("=", 1)[0] in {"8333", "8888"}
    )
    ingress_states = dict(
        line.split("=", 1)
        for line in (ingress_out or "").splitlines()
        if "=" in line and line.split("=", 1)[0] in {"8333", "8888"}
    )
    expected_ports = {"8333", "8888"}
    peer_blocked = (
        peer_rc == 0
        and peer_states.keys() == expected_ports
        and all(state == "CLOSED" for state in peer_states.values())
    )
    ingress_allowed = (
        ingress_rc == 0
        and ingress_states.keys() == expected_ports
        and all(state == "OPEN" for state in ingress_states.values())
    )
    passed = peer_blocked and ingress_allowed
    return VerifyCheck(
        "seaweedfs",
        "s3_host_exposure",
        passed,
        (
            f"{peer_node} blocked; {ingress_node} ingress allowed"
            if passed
            else (
                f"{peer_node}={'blocked' if peer_blocked else 'open'}; "
                f"{ingress_node}={'allowed' if ingress_allowed else 'blocked'}"
            )
        ),
    )


def _check_seaweedfs_buckets(cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
    """List S3 buckets via ``weed shell`` and verify expected buckets exist."""
    import subprocess

    from toolkit.services.sdk import VerifyCheck, ssh_on_vm
    from toolkit.services.seaweedfs.bootstrap import SEAWEEDFS_EXPECTED_BUCKETS

    if not cfg.category_enabled("cloud"):
        return VerifyCheck("seaweedfs", "buckets", True, "cloud not enabled")
    from toolkit.core.manifest.placement import service_address

    service_ip = service_address(cfg, "seaweedfs") if cfg.is_multi_node else "localhost"
    shell_cmd = "docker exec -i seaweedfs weed shell"
    if cfg.is_multi_node:
        rc, out, _ = ssh_on_vm(cfg, service_ip, f"echo 's3.bucket.list' | {shell_cmd}", root=root, timeout=60)
        out = out or ""
    else:
        try:
            proc = subprocess.run(
                ["sh", "-c", f"echo 's3.bucket.list' | {shell_cmd}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            rc, out = proc.returncode, (proc.stdout + proc.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return VerifyCheck("seaweedfs", "buckets", False, f"weed shell failed: {exc}")
    if rc != 0 or not out.strip():
        return VerifyCheck("seaweedfs", "buckets", False, "weed shell unreachable")
    listed = {ln.split("\t", 1)[0].strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("s3.bucket")}
    expected = set(SEAWEEDFS_EXPECTED_BUCKETS)
    missing = sorted(expected - listed)
    if missing:
        detail = f"missing: {', '.join(missing)} (have: {', '.join(sorted(listed)) or 'none'})"
        return VerifyCheck("seaweedfs", "buckets", False, detail)
    detail = f"{len(listed)} bucket(s): {', '.join(sorted(listed & expected))}"
    return VerifyCheck("seaweedfs", "buckets", True, detail)


def _bucket_snapshot(
    cfg: Config,
    vm_ip: str,
    root: Path,
) -> list[dict[str, object]]:
    """Read the bucket inventory through SeaweedFS' local read-only shell."""
    from toolkit.services.sdk import docker_exec_on_vm

    rc, output = docker_exec_on_vm(
        cfg,
        "seaweedfs",
        [
            "sh",
            "-c",
            "{ weed shell; printf '\\n__HOMELAB_BUCKET_RC__%s\\n' \"$?\"; } 2>&1 | head -c 65537",
        ],
        vm_ip,
        root,
        timeout=8,
        stdin="s3.bucket.list\n",
    )
    if rc != 0 or len(output.encode("utf-8", errors="replace")) > _BUCKET_OUTPUT_LIMIT:
        raise RuntimeError("SeaweedFS bucket inventory is unavailable")
    rows: list[dict[str, object]] = []
    command_rc: int | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(_BUCKET_RC_MARKER):
            try:
                command_rc = int(stripped.removeprefix(_BUCKET_RC_MARKER))
            except ValueError as exc:
                raise RuntimeError("SeaweedFS bucket inventory is unavailable") from exc
            continue
        if not stripped or stripped.startswith((">", "s3.bucket")):
            continue
        if stripped.lower().startswith(("error", "failed", "fatal")):
            raise RuntimeError("SeaweedFS bucket inventory is unavailable")
        if "\t" not in stripped:
            continue
        raw_name = stripped.split("\t", 1)[0]
        name = "".join(char for char in raw_name if ord(char) >= 32 and ord(char) != 127)
        if name:
            rows.append({"name": name[:120]})
    if command_rc != 0:
        raise RuntimeError("SeaweedFS bucket inventory is unavailable")
    return rows


class SeaweedfsPlugin(ServicePlugin):
    service = "seaweedfs"
    category = "cloud"

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        """Expose a bounded, read-only S3 bucket inventory."""
        return {"buckets": _bucket_snapshot(cfg, self.runtime_address(cfg), root)[:_RESOURCE_LIMIT]}

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        context.render_template(
            "generated/seaweedfs-s3.json",
            "seaweedfs-s3.json.j2",
            {
                "access_key": context.secrets.get("SEAWEEDFS_S3_ACCESS_KEY", "admin"),
                "secret_key": context.secrets.get("SEAWEEDFS_S3_SECRET_KEY", "unset"),
            },
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        from toolkit.services.seaweedfs.bootstrap import bootstrap_seaweedfs_buckets

        return bootstrap_seaweedfs_buckets(cfg, secrets)

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """SeaweedFS cluster, filer, S3 status, host exposure, and expected buckets."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm

        if cfg.domain == "localhost":
            return [VerifyCheck("seaweedfs", "s3_status", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "seaweedfs", root):
            return [VerifyCheck("seaweedfs", "s3_status", False, "container missing")]
        return [
            _check_seaweedfs_cluster(cfg, vm_ip, root),
            _check_seaweedfs_filer(cfg, vm_ip, root),
            _check_seaweedfs_s3(cfg, vm_ip, root),
            _check_seaweedfs_s3_auth(cfg, vm_ip, root, secrets),
            _check_seaweedfs_s3_host_exposure(cfg, vm_ip, root),
            _check_seaweedfs_buckets(cfg, vm_ip, root),
        ]
