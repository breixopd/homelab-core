"""ntfy service plugin.

Custom verify logic for the ntfy notification service. The base
ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml; this file overrides only what
needs custom Python logic (verify, post_start, oidc_client, heal).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck

_VERIFY_TOPIC = "homelab-verify-probe"


class NtfyPlugin(ServicePlugin):
    service = "ntfy"
    category = "notifications"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        from toolkit.services.ntfy.client import NtfyClient, resolve_local_ntfy_base

        logs: list[str] = []
        client = NtfyClient(resolve_local_ntfy_base())
        for topic in ("alerts", "backups", "container-updates", "arr-sonarr", "arr-radarr"):
            for attempt in range(3):
                if client.send(topic, "Homelab toolkit initialized", title="Setup", priority="low"):
                    logs.append(f"Initialized ntfy topic: {topic}")
                    break
                if attempt < 2:
                    time.sleep(2)
            else:
                logs.append(f"ntfy topic {topic} will initialize on first publish")
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl, docker_exec_on_vm

        if cfg.domain == "localhost":
            return [VerifyCheck("ntfy", "roundtrip", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "ntfy", root):
            return [VerifyCheck("ntfy", "roundtrip", False, "container missing")]

        checks: list[VerifyCheck] = []
        rc, _body = docker_curl(cfg, vm_ip, "ntfy", "http://localhost/v1/health", root=root)
        if rc != 0:
            return [VerifyCheck("ntfy", "roundtrip", False, "health endpoint unreachable")]

        marker = f"verify-{int(time.time())}"
        pub_cmd = (
            f"wget -qO- --post-data={json.dumps(marker)} "
            f"--header='Title: Verify' --header='Priority: min' "
            f"http://127.0.0.1/{_VERIFY_TOPIC}"
        )
        rc2, _out = docker_exec_on_vm(cfg, "ntfy", ["sh", "-c", pub_cmd], vm_ip, root, timeout=15)
        if rc2 != 0:
            checks.append(VerifyCheck("ntfy", "publish", False, "POST failed"))
            return checks
        checks.append(VerifyCheck("ntfy", "publish", True, "publish ok"))

        rc3, body = docker_curl(
            cfg,
            vm_ip,
            "ntfy",
            f"http://localhost/{_VERIFY_TOPIC}/json?poll=1&since=30s",
            root=root,
            timeout=15,
        )
        roundtrip_ok = False
        detail = "poll failed"
        if rc3 == 0 and body:
            # ntfy's /json poll endpoint returns NDJSON: one JSON object per line.
            messages: list[dict] = []
            invalid = False
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    invalid = True
                    continue
                if isinstance(obj, dict):
                    messages.append(obj)
            if messages:
                roundtrip_ok = any(marker in str(m.get("message", "")) for m in messages)
                detail = "message received" if roundtrip_ok else "published message not found in poll"
            elif invalid:
                detail = "invalid poll JSON"
        checks.append(VerifyCheck("ntfy", "roundtrip", roundtrip_ok, detail))
        return checks
