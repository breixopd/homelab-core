"""Redis Exporter service plugin with authenticated scrape verification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


class RedisExporterPlugin(ServicePlugin):
    service = "redis-exporter"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.manifest.variables import compile_manifest_integration_variables

        password = quote(context.secrets.get("REDIS_PASSWORD", ""), safe="")
        variables = compile_manifest_integration_variables(context.config, context.manifest)
        host = variables["REDIS_EXPORTER_REDIS_HOST"]
        port = variables["REDIS_EXPORTER_REDIS_PORT"]
        context.write_text(
            "generated/.env.redis-exporter",
            f"REDIS_ADDR=redis://default:{password}@{host}:{port}\n",
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require a successful authenticated Redis scrape and server statistics."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        if not container_exists_on_vm(cfg, vm_ip, self.service, root):
            return [VerifyCheck(self.service, "redis_scrape", False, "container missing")]
        rc, body = docker_curl(cfg, vm_ip, self.service, "http://localhost:9121/metrics", root=root, timeout=15)
        up = bool(re.search(r"^redis_up(?:\{[^}]*\})?\s+1(?:\.0+)?$", body or "", re.MULTILINE))
        statistics = "redis_connected_clients" in (body or "") and "redis_memory_used_bytes" in (body or "")
        passed = rc == 0 and up and statistics
        return [
            VerifyCheck(
                self.service,
                "redis_scrape",
                passed,
                (
                    "redis_up=1 with client and memory statistics"
                    if passed
                    else "authenticated Redis scrape is incomplete"
                ),
            )
        ]
