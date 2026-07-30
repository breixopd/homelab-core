"""gluetun service plugin.

Owns its verify() (VPN tunnel health + egress IP differs from host) on top of
the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services import RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
logger = logging.getLogger(__name__)


class GluetunPlugin(ServicePlugin):
    service = "gluetun"
    category = "media"

    def prepare_bootstrap_credentials(self, cfg: Config, credentials: dict[str, str]) -> dict[str, str]:
        provider = str(self.setting(cfg, "provider"))
        return {
            "VPN_PROVIDER": provider,
            "VPN_TYPE": "wireguard" if provider == "nordvpn" else "openvpn",
        }

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.ops.vpn import build_vpn_env, resolve_vpn_type

        secrets = dict(context.secrets)
        provider = str(self.setting(context.config, "provider"))
        secrets["VPN_PROVIDER"] = provider
        configured_provider = str(context.secrets.get("VPN_PROVIDER") or "").strip().lower()
        secrets["VPN_TYPE"] = (
            str(context.secrets.get("VPN_TYPE") or "").strip().lower()
            if configured_provider == provider
            else ("wireguard" if provider == "nordvpn" else "openvpn")
        )
        if not secrets["VPN_TYPE"]:
            secrets["VPN_TYPE"] = resolve_vpn_type(provider)
        try:
            vpn_vars, derived_key = build_vpn_env(secrets)
        except Exception:
            logger.warning("VPN key derivation failed; generating the available configuration", exc_info=True)
            vpn_vars = {
                "VPN_SERVICE_PROVIDER": (secrets.get("VPN_PROVIDER") or "").strip().lower(),
                "VPN_TYPE": (secrets.get("VPN_TYPE") or "").strip().lower(),
                "OPENVPN_USER": secrets.get("VPN_USER", ""),
                "OPENVPN_PASSWORD": secrets.get("VPN_PASSWORD", ""),
                "SERVER_COUNTRIES": secrets.get("VPN_SERVER_COUNTRIES", ""),
                "WIREGUARD_PRIVATE_KEY": secrets.get("WIREGUARD_PRIVATE_KEY", ""),
                "WIREGUARD_ADDRESSES": secrets.get("WIREGUARD_ADDRESSES", ""),
            }
            derived_key = ""
        from toolkit.core.generate.artifacts import render_env_value

        context.write_text(
            "generated/.env.vpn",
            "".join(f"{key}={render_env_value(value)}\n" for key, value in vpn_vars.items()),
        )
        if derived_key:
            try:
                from toolkit.core.secrets.secrets import merge_secret_values

                merge_secret_values(context.root, {"WIREGUARD_PRIVATE_KEY": derived_key})
            except Exception:
                logger.warning("Failed to cache the derived VPN key", exc_info=True)

    def prepare_runtime_deployment(
        self,
        context: RuntimeLifecycleContext,
        services: tuple[str, ...],
    ) -> None:
        vpn_env = context.root / "generated" / ".env.vpn"
        node = context.node if isinstance(context.node, str) else ""
        if not _vpn_credentials_ready(vpn_env) and node:
            node_env = context.root / "generated" / node / ".env"
            if _repair_vpn_credentials(vpn_env, node_env):
                context.log("Repaired Gluetun credentials from the node-scoped runtime environment")
        if not _vpn_credentials_ready(vpn_env):
            raise RuntimeError("Gluetun is enabled but VPN credentials are incomplete")
        context.set_state("gluetun_healthy", context.services_healthy(services))

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        context.set_state("gluetun_healthy", context.services_healthy(services))

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Gluetun control-server health and VPN egress IP must differ from host."""
        import subprocess

        from toolkit.core.manifest.settings import service_enabled
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm, ssh_on_vm

        if not service_enabled(cfg, self.service):
            return [VerifyCheck("gluetun", "tunnel", True, "skipped (VPN off)")]
        if not container_exists_on_vm(cfg, vm_ip, "gluetun", root):
            return [VerifyCheck("gluetun", "tunnel", False, "container missing")]

        hrc, _ = docker_exec_on_vm(cfg, "gluetun", ["wget", "-qO-", "http://127.0.0.1:9999"], vm_ip, root, timeout=15)
        health_check = VerifyCheck(
            "gluetun",
            "health",
            hrc == 0,
            "control server ok" if hrc == 0 else "control server unreachable",
        )

        rc, out = docker_exec_on_vm(cfg, "gluetun", ["wget", "-qO-", "https://ipinfo.io/ip"], vm_ip, root, timeout=30)
        vpn_ip = (out or "").strip().splitlines()[-1].strip() if out else ""
        vpn_ok = rc == 0 and bool(_IP_RE.match(vpn_ip))
        if not vpn_ok:
            return [health_check, VerifyCheck("gluetun", "egress", False, (out or "egress check failed")[:120])]

        host_ip = ""
        if cfg.is_multi_node:
            hrc2, hout, _ = ssh_on_vm(
                cfg,
                vm_ip,
                "curl -sf https://ipinfo.io/ip 2>/dev/null || wget -qO- https://ipinfo.io/ip",
                root=root,
                timeout=20,
            )
            if hrc2 == 0 and hout:
                host_ip = hout.strip().splitlines()[-1].strip()
        else:
            try:
                proc = subprocess.run(
                    ["curl", "-sf", "https://ipinfo.io/ip"], capture_output=True, text=True, timeout=15, check=False
                )
                host_ip = (proc.stdout or "").strip()
            except OSError:
                host_ip = ""

        if host_ip and _IP_RE.match(host_ip) and host_ip == vpn_ip:
            egress = VerifyCheck("gluetun", "egress", False, f"VPN IP equals host IP ({vpn_ip}) — tunnel may be down")
        else:
            detail = f"VPN IP {vpn_ip}"
            if host_ip and _IP_RE.match(host_ip):
                detail += f" (host {host_ip})"
            egress = VerifyCheck("gluetun", "egress", True, detail)
        return [health_check, egress]


def _vpn_credentials_ready(vpn_env: Path) -> bool:
    if not vpn_env.is_file():
        return False
    values = {
        key: value
        for line in vpn_env.read_text(encoding="utf-8").splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    return bool(values.get("OPENVPN_USER") or values.get("WIREGUARD_PRIVATE_KEY"))


def _repair_vpn_credentials(vpn_env: Path, node_env: Path) -> bool:
    if not node_env.is_file():
        return False
    from dotenv import dotenv_values
    from toolkit.core.generate.artifacts import render_env_value
    from toolkit.core.ops.vpn import build_vpn_env
    from toolkit.core.state.files import atomic_write_text

    values = {
        key: value
        for key, value in dotenv_values(node_env).items()
        if isinstance(value, str)
    }
    try:
        vpn_vars, _derived_key = build_vpn_env(values)
    except Exception:
        logger.warning("VPN credential repair from the node environment failed", exc_info=True)
        return False
    if not (vpn_vars.get("OPENVPN_USER") or vpn_vars.get("WIREGUARD_PRIVATE_KEY")):
        return False
    atomic_write_text(
        vpn_env,
        "".join(f"{key}={render_env_value(value)}\n" for key, value in vpn_vars.items()),
        mode=0o600,
    )
    return _vpn_credentials_ready(vpn_env)
