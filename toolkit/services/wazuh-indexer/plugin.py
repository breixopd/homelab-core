"""wazuh-indexer service plugin — OpenSearch backend for Wazuh SIEM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import bcrypt
from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services import RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


def _stable_bcrypt_hash(password: str, existing: str) -> str:
    if not password:
        raise ValueError("Wazuh generated configuration requires a non-empty password")
    if existing:
        try:
            if bcrypt.checkpw(password.encode(), existing.encode()):
                return existing
        except ValueError:
            pass
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


class WazuhIndexerPlugin(ServicePlugin):
    service = "wazuh-indexer"
    category = "security"

    def ansible_secret_variables(self, cfg: Config, secrets: dict[str, str]) -> dict[str, str]:
        """Provide manager credentials through the owner-only ephemeral Ansible vars file."""
        return {
            "wazuh_api_password": secrets.get("WAZUH_API_PASSWORD", ""),
            "wazuh_indexer_password": secrets.get("WAZUH_INDEXER_PASSWORD", ""),
        }

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        import yaml

        relative = "generated/wazuh/internal_users.yml"
        path = context.artifact_path(relative)
        existing_admin = ""
        existing_dashboard = ""
        if path.is_file() and not path.is_symlink():
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(document, dict):
                    admin = document.get("admin")
                    dashboard = document.get("kibanaserver")
                    if isinstance(admin, dict) and isinstance(admin.get("hash"), str):
                        existing_admin = admin["hash"]
                    if isinstance(dashboard, dict) and isinstance(dashboard.get("hash"), str):
                        existing_dashboard = dashboard["hash"]
            except (OSError, UnicodeError, yaml.YAMLError):
                pass

        indexer_password = context.secrets.get("WAZUH_INDEXER_PASSWORD", "")
        dashboard_password = context.secrets.get("WAZUH_DASHBOARD_PASSWORD", indexer_password)
        context.render_template(
            relative,
            "wazuh-internal-users.yml.j2",
            {
                "admin_hash": _stable_bcrypt_hash(indexer_password, existing_admin),
                "kibanaserver_hash": _stable_bcrypt_hash(dashboard_password, existing_dashboard),
            },
        )

    def before_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> tuple[str, ...]:
        context.run_recovery(
            "ensure_wazuh_indexer_healthy",
            "toolkit.services.wazuh-indexer.bootstrap",
        )
        return services

    def supported_actions(self) -> frozenset[str]:
        return frozenset({"reconcile-security"})

    def execute_action(
        self,
        action: str,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> list[str]:
        if action != "reconcile-security":
            raise ValueError("unsupported wazuh-indexer action")
        import importlib

        reconcile_wazuh_security = importlib.import_module(
            "toolkit.services.wazuh-indexer.bootstrap"
        ).reconcile_wazuh_security

        return reconcile_wazuh_security(cfg, root)

    def verify(self, cfg: Config, secrets: dict, vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Authenticated cluster health + wazuh alert indices present."""
        from toolkit.services.sdk import (
            VerifyCheck,
            VerifyStatus,
            basic_auth_header,
            container_exists_on_vm,
            docker_curl,
        )

        if cfg.domain == "localhost" or not cfg.category_enabled("security"):
            return [VerifyCheck("wazuh-indexer", "skipped", True, "skipped (localhost or security disabled)")]

        password = secrets.get("WAZUH_INDEXER_PASSWORD", "")
        if not password:
            return [VerifyCheck("wazuh-indexer", "cluster_health", False, "WAZUH_INDEXER_PASSWORD not set")]

        if not container_exists_on_vm(cfg, vm_ip, "wazuh-indexer", root):
            return [VerifyCheck("wazuh-indexer", "cluster_health", False, "container missing")]

        headers = {"Authorization": basic_auth_header("admin", password)}
        ca_file = "/usr/share/wazuh-indexer/config/certs/root-ca.pem"
        rc, out = docker_curl(
            cfg,
            vm_ip,
            "wazuh-indexer",
            "https://wazuh.indexer:9200/_cluster/health",
            root=root,
            headers=headers,
            ca_file=ca_file,
            timeout=15,
        )
        status = ""
        health_ok = False
        detail = (out or "unreachable")[:120]
        if rc == 0 and out:
            try:
                status = (json.loads(out).get("status") or "").lower()
            except json.JSONDecodeError:
                status = ""
            if status in ("green", "yellow"):
                health_ok = True
                detail = f"status={status}"
            elif status == "red":
                health_ok = False
                detail = "cluster status red"
            else:
                detail = f"unexpected response: {out[:80]}"

        checks: list[VerifyCheck] = [
            VerifyCheck("wazuh-indexer", "cluster_health", health_ok, detail),
        ]

        rc_idx, idx_out = docker_curl(
            cfg,
            vm_ip,
            "wazuh-indexer",
            "https://wazuh.indexer:9200/_cat/indices/wazuh-alerts-*?h=index",
            root=root,
            headers=headers,
            ca_file=ca_file,
            timeout=15,
        )
        indices = [ln.strip() for ln in (idx_out or "").splitlines() if ln.strip()]
        if rc_idx != 0:
            alert_ok = False
            alert_detail = "alert indices probe failed"
        elif indices:
            alert_detail = f"{len(indices)} wazuh-alerts index(es)"
            alert_ok = True
        elif health_ok:
            alert_ok = False
            alert_detail = "no wazuh-alerts indices yet"
        else:
            alert_ok = False
            alert_detail = "no wazuh-alerts indices"
        checks.append(
            VerifyCheck(
                "wazuh-indexer",
                "alert_indices",
                alert_ok,
                alert_detail,
                status=VerifyStatus.NOT_READY if not alert_ok and not indices and rc_idx == 0 else None,
            )
        )
        return checks
