"""qbittorrent service plugin.

Owns its verify() (Web UI enforces auth from network clients) on top of the
base ServicePlugin defaults read from its manifest and Compose application.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services import RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


class QbittorrentPlugin(ServicePlugin):
    service = "qbittorrent"
    category = "media"
    heal_aliases = ("qbittorrent-vpn",)

    def before_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> tuple[str, ...]:
        if "qbittorrent-vpn" not in services:
            return services
        # The VPN variant is a separate Compose service.  Removing the plain
        # service leaves a stale ``container:<old-gluetun-id>`` namespace on
        # the active qBittorrent container after Gluetun is recreated.
        for container in ("qbittorrent-vpn", "qbittorrent"):
            context.run_host(["docker", "rm", "-f", container])
        if context.state("gluetun_healthy", False):
            context.add_compose_up_option("--no-recreate")
        return services

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        if "qbittorrent-vpn" in services:
            context.remove_compose_up_option("--no-recreate")

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Rotate qBittorrent WebUI credentials to the secrets value."""
        import importlib

        bootstrap = importlib.import_module("toolkit.services.qbittorrent.bootstrap")
        from toolkit.core.manifest.settings import service_enabled, service_setting_bool

        vpn_enabled = service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")
        service_host = "gluetun" if vpn_enabled else "qbittorrent"
        return bootstrap.bootstrap_qbittorrent_credentials(secrets, service_host=service_host)

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify qBittorrent auth enforcement, API login, save path, and VPN egress."""
        from toolkit.core.manifest.settings import service_enabled, service_setting_bool
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm

        vpn_enabled = service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")
        container = "qbittorrent-vpn" if vpn_enabled else "qbittorrent"
        try:
            if not container_exists_on_vm(cfg, vm_ip, container, root):
                checks = [VerifyCheck("qbittorrent", "container", False, f"{container} not found")]
                if not vpn_enabled:
                    checks.append(self._check_vpn_egress(cfg, vm_ip, root))
                return checks
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [VerifyCheck("qbittorrent", "container", False, f"container check failed: {exc}")]

        checks = [self._verify_auth(cfg, vm_ip, root)]
        checks.extend(self._check_authenticated_api(cfg, secrets, vm_ip, root))
        checks.append(self._check_vpn_egress(cfg, vm_ip, root))
        return checks

    def _verify_auth(self, cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """Verify qBittorrent web UI requires authentication from network clients."""
        from toolkit.core.manifest.settings import service_enabled, service_setting_bool
        from toolkit.services.sdk import VerifyCheck, ssh_on_vm

        vpn_enabled = service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")
        container = "qbittorrent-vpn" if vpn_enabled else "qbittorrent"
        # A container can be attached to several scoped networks.  Keep a
        # delimiter between addresses; concatenating them creates an invalid
        # host (curl reports this as HTTP 000/0), hiding a healthy WebUI.
        ip_template = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"

        def _resolve_ip(run_fn):
            target = container
            # Compose's ``network_mode: service:gluetun`` is materialised by
            # Docker as ``container:<id>``.  The qBittorrent container itself
            # has no network IP in that mode; follow the chain to the shared
            # network namespace before resolving the address.
            for _ in range(3):
                rc_parent, parent_out, _ = run_fn(
                    ["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", target], 15
                )
                if rc_parent != 0:
                    return ""
                mode = (parent_out or "").strip()
                if mode.startswith(("service:", "container:")):
                    target = mode.split(":", 1)[1]
                    continue
                break
            rc_ip, ip_out, _ = run_fn(["docker", "inspect", "-f", ip_template, target], 15)
            return (ip_out or "").strip().split()[0] if (ip_out or "").strip() else ""

        if cfg.is_multi_node:

            def run_fn(cmd, timeout):
                return ssh_on_vm(cfg, vm_ip, shlex.join(cmd), root=root, timeout=timeout)

            container_ip = _resolve_ip(run_fn)
            if not container_ip:
                return VerifyCheck("qbittorrent", "auth", False, f"container {container} not found or no IP")
            probe = f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 8 http://{shlex.quote(container_ip)}:8080/"
            rc, out, _ = ssh_on_vm(cfg, vm_ip, probe, root=root, timeout=20)
        else:

            def run_fn(cmd, timeout):
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
                return proc.returncode, proc.stdout, proc.stderr

            try:
                container_ip = _resolve_ip(run_fn)
                if not container_ip:
                    return VerifyCheck("qbittorrent", "auth", False, f"container {container} not found or no IP")
                proc = subprocess.run(
                    [
                        "curl",
                        "-sS",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "--max-time",
                        "8",
                        f"http://{container_ip}:8080/",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                rc, out = proc.returncode, proc.stdout + proc.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                return VerifyCheck("qbittorrent", "auth", False, f"probe failed: {exc}")

        code = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if not code.isdigit():
            return VerifyCheck("qbittorrent", "auth", False, f"probe failed (rc={rc}, out={(out or '')[:80]})")
        status = int(code)
        ok = status in (301, 302, 307, 308, 401, 403)
        detail = f"HTTP {status}"
        if status in (301, 302, 307, 308):
            detail += " (redirect to login)"
        elif status == 401:
            detail += " (auth required)"
        elif status == 403:
            detail += " (API auth enforced)"
        elif status == 200:
            if cfg.is_multi_node:
                api_probe = (
                    f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 8 "
                    f"http://{shlex.quote(container_ip)}:8080/api/v2/app/version"
                )
                rc2, out2, _ = ssh_on_vm(cfg, vm_ip, api_probe, root=root, timeout=20)
            else:
                proc2 = subprocess.run(
                    [
                        "curl",
                        "-sS",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "--max-time",
                        "8",
                        f"http://{container_ip}:8080/api/v2/app/version",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                rc2, out2 = proc2.returncode, proc2.stdout + proc2.stderr
            api_code = (out2 or "").strip().splitlines()[-1].strip() if (out2 or "").strip() else ""
            if api_code == "403":
                ok = True
                detail += " — SPA shell (API returns 403, auth enforced)"
            elif api_code == "200":
                detail += " — web UI served without auth!"
            else:
                detail += f" — API probe returned {api_code} (rc={rc2})"
        return VerifyCheck("qbittorrent", "auth", ok, detail)

    def _check_authenticated_api(
        self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path
    ) -> list[VerifyCheck]:
        """Login with stored creds; verify API works and save path is writable."""
        from urllib.parse import urlencode

        from toolkit.services.sdk import VerifyCheck, docker_curl, docker_exec_on_vm

        user = (secrets.get("QBITTORRENT_USER") or "admin").strip() or "admin"
        password = secrets.get("QBITTORRENT_PASSWORD", "")
        if not password:
            return [VerifyCheck("qbittorrent", "api_login", False, "QBITTORRENT_PASSWORD not set")]

        from toolkit.core.manifest.settings import service_enabled, service_setting_bool

        vpn_enabled = service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")
        container = "qbittorrent-vpn" if vpn_enabled else "qbittorrent"
        login_rc, login_out = docker_curl(
            cfg,
            vm_ip,
            container,
            "http://127.0.0.1:8080/api/v2/auth/login",
            root=root,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({"username": user, "password": password}),
            cookie_jar="/tmp/qbit-cookie",
            timeout=25,
        )
        if login_rc != 0 or "Fails." in (login_out or "") or "Unauthorized" in (login_out or ""):
            return [VerifyCheck("qbittorrent", "api_login", False, "authenticated API unreachable")]
        rc, out = docker_curl(
            cfg,
            vm_ip,
            container,
            "http://127.0.0.1:8080/api/v2/app/preferences",
            root=root,
            cookie_file="/tmp/qbit-cookie",
            timeout=25,
        )
        if rc != 0 or not out:
            return [VerifyCheck("qbittorrent", "api_login", False, "authenticated API unreachable")]

        try:
            prefs = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return [VerifyCheck("qbittorrent", "api_login", False, "invalid preferences JSON")]

        save_path = str(prefs.get("save_path", "") or prefs.get("savePath", ""))
        checks = [VerifyCheck("qbittorrent", "api_login", True, "session ok")]
        if not save_path:
            checks.append(VerifyCheck("qbittorrent", "save_path", False, "save_path unset"))
            return checks

        candidate_paths = [save_path]
        if save_path == "/downloads":
            candidate_paths.append("/data/downloads")
        writable_path = ""
        for candidate in dict.fromkeys(candidate_paths):
            test_cmd = ["sh", "-c", f"test -d {shlex.quote(candidate)} && test -w {shlex.quote(candidate)}"]
            wrc, _ = docker_exec_on_vm(cfg, container, test_cmd, vm_ip, root, timeout=15)
            if wrc == 0:
                writable_path = candidate
                break
        checks.append(
            VerifyCheck(
                "qbittorrent",
                "save_path",
                bool(writable_path),
                f"{writable_path or save_path} writable" if writable_path else f"{save_path} missing or read-only",
            )
        )
        return checks

    def _check_vpn_egress(self, cfg: Config, vm_ip: str, root: Path) -> VerifyCheck:
        """When VPN profile is on, qBittorrent egress must differ from host public IP."""
        from toolkit.core.manifest.settings import service_enabled, service_setting_bool
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm, ssh_on_vm

        if not (service_enabled(cfg, "gluetun") and service_setting_bool(cfg, "gluetun", "enabled")):
            return VerifyCheck("qbittorrent", "vpn_egress", True, "skipped (VPN off)")

        try:
            if not container_exists_on_vm(cfg, vm_ip, "gluetun", root):
                return VerifyCheck("qbittorrent", "vpn_egress", False, "gluetun not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return VerifyCheck("qbittorrent", "vpn_egress", False, f"gluetun check failed: {exc}")

        rc, out = docker_exec_on_vm(cfg, "gluetun", ["wget", "-qO-", "https://ipinfo.io/ip"], vm_ip, root, timeout=30)
        vpn_ip = (out or "").strip().splitlines()[-1].strip() if out else ""
        ip_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        if rc != 0 or not ip_re.match(vpn_ip):
            return VerifyCheck("qbittorrent", "vpn_egress", False, "gluetun egress probe failed")

        host_ip = ""
        if cfg.is_multi_node:
            hrc, hout, _ = ssh_on_vm(
                cfg,
                vm_ip,
                "curl -sf https://ipinfo.io/ip 2>/dev/null || wget -qO- https://ipinfo.io/ip",
                root=root,
                timeout=20,
            )
            if hrc == 0 and hout:
                host_ip = hout.strip().splitlines()[-1].strip()
        else:
            try:
                proc = subprocess.run(
                    ["curl", "-sf", "https://ipinfo.io/ip"], capture_output=True, text=True, timeout=15, check=False
                )
                host_ip = (proc.stdout or "").strip()
            except OSError:
                host_ip = ""

        if host_ip and ip_re.match(host_ip) and host_ip == vpn_ip:
            return VerifyCheck("qbittorrent", "vpn_egress", False, f"VPN IP equals host ({vpn_ip})")
        return VerifyCheck("qbittorrent", "vpn_egress", True, f"VPN egress {vpn_ip}")

    def heal(self, cfg: Config, root: Path, *, service: str | None = None) -> list[str] | None:
        container = service or self.service
        if container not in (self.service, *self.heal_aliases):
            return None
        return _heal_qbittorrent_stale_lock(root, container)


def _heal_qbittorrent_stale_lock(root: Path, container: str) -> list[str]:
    """Clear stale qBittorrent lock files and restart the container."""
    logs: list[str] = [f"HEAL {container}: clearing stale lockfile and restarting"]
    cfg_dir = root / "config" / "qbittorrent" / "qBittorrent"
    try:
        subprocess.run(["docker", "stop", container], capture_output=True, timeout=60, check=False)
        for stale in ("lockfile", "ipc-socket"):
            path = cfg_dir / stale
            if path.exists():
                path.unlink()
                logs.append(f"  removed {path.name}")
        proc = subprocess.run(["docker", "start", container], capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode == 0:
            logs.append(f"  OK: {container} restarted with clean lock state")
        else:
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            logs.append(f"  FAIL: {container} start failed: {out[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logs.append(f"  ERROR: {container}: {exc}")
    return logs
