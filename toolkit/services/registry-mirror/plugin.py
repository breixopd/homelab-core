"""registry-mirror service plugin.

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
    from toolkit.services import RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


class RegistryMirrorPlugin(ServicePlugin):
    service = "registry-mirror"
    category = "management"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Install the mirror CA and reconcile the host Docker daemon configuration."""
        import importlib
        import subprocess

        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT

        try:
            bootstrap = importlib.import_module("toolkit.services.registry-mirror.bootstrap")
            return bootstrap.ensure_registry_mirror(root or Path(DEFAULT_HOMELAB_ROOT))
        except (OSError, subprocess.SubprocessError) as exc:
            return [f"WARNING: Registry mirror not ready yet ({exc})"]

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        import importlib

        from toolkit.services.sdk import registry_mirror_ca_url

        url = registry_mirror_ca_url(context.environment("PRIVATE_IP", "127.0.0.1"))
        proc = context.run_host(["curl", "-sf", "-o", "/dev/null", "--connect-timeout", "3", url])
        if proc.returncode == 0:
            return
        context.warn("registry-mirror not responding; purging cache and retrying")

        bootstrap = importlib.import_module("toolkit.services.registry-mirror.bootstrap")
        for line in bootstrap.purge_registry_mirror_cache():
            context.log(line)
        if not context.retry_services(("registry-mirror",)):
            context.record_failure()

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Registry mirror running, CA cert, /v2/ proxy path, and pull-through probe."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            docker_health_status_on_vm,
            registry_mirror_ca_url,
            registry_mirror_port,
            registry_mirror_running,
            ssh_on_vm,
        )

        port = registry_mirror_port()
        mirror_host = vm_ip if cfg.is_multi_node else "127.0.0.1"
        ca_url = registry_mirror_ca_url(mirror_host)

        if cfg.domain == "localhost":
            return [VerifyCheck("registry-mirror", "health", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "registry-mirror", root):
            return [VerifyCheck("registry-mirror", "health", False, "container missing")]

        checks: list[VerifyCheck] = []
        _state, health = (
            docker_health_status_on_vm(cfg, vm_ip, "registry-mirror", root) if cfg.is_multi_node else ("", "")
        )
        running = health == "healthy" or registry_mirror_running()
        checks.append(
            VerifyCheck(
                "registry-mirror",
                "running",
                running,
                "docker health healthy" if health == "healthy" else ("running" if running else "not running"),
            )
        )

        probe_cmd = f"curl -sf --max-time 5 {ca_url} -o /dev/null && echo OK || echo FAIL"
        if cfg.is_multi_node:
            rc, out, _ = ssh_on_vm(cfg, vm_ip, probe_cmd, root=root, timeout=15)
            ca_ok = rc == 0 and "OK" in (out or "")
        else:
            import subprocess

            proc = subprocess.run(
                ["curl", "-sf", "--max-time", "5", ca_url, "-o", "/dev/null"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            ca_ok = proc.returncode == 0
        checks.append(
            VerifyCheck(
                "registry-mirror",
                "ca_cert",
                ca_ok,
                f"CA cert on port {port}" if ca_ok else "CA cert unreachable",
            )
        )

        rc, body = docker_curl(
            cfg,
            vm_ip,
            "registry-mirror",
            f"http://127.0.0.1:{port}/v2/",
            root=root,
        )
        v2_ok = rc == 0 or (body or "").strip() in ("{}", "") or "401" in (body or "")
        checks.append(
            VerifyCheck(
                "registry-mirror",
                "v2_endpoint",
                v2_ok,
                "registry /v2/ reachable via mirror" if v2_ok else "mirror /v2/ probe failed",
            )
        )

        pull_probe = "wget -qS -O /dev/null https://registry-1.docker.io/v2/ 2>&1 | grep -o ' 401' | tr -d ' '"
        pull_rc, pull_out = docker_exec_on_vm(
            cfg,
            "registry-mirror",
            ["sh", "-c", pull_probe],
            vm_ip,
            root,
            timeout=20,
        )
        code = ((pull_out or "").strip().splitlines() or [""])[0].strip()
        pull_ok = pull_rc == 0 and code in ("200", "401")
        checks.append(
            VerifyCheck(
                "registry-mirror",
                "pull_through",
                pull_ok,
                f"docker.io via proxy HTTP {code or 'fail'}" if pull_ok else "pull-through probe failed",
            )
        )

        return checks
