"""Postgres Exporter service plugin with database scrape verification."""

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


class PostgresExporterPlugin(ServicePlugin):
    service = "postgres-exporter"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        username = quote(context.secrets.get("POSTGRES_USER", "admin"), safe="")
        password = quote(context.secrets.get("POSTGRES_PASSWORD", ""), safe="")
        host = context.secrets.get("POSTGRES_HOST", "postgres")
        port = context.secrets.get("POSTGRES_PORT", "5432")
        context.write_text(
            "generated/.env.postgres-exporter",
            f"DATA_SOURCE_NAME=postgresql://{username}:{password}@{host}:{port}/postgres?sslmode=disable\n",
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require a successful database scrape and emitted PostgreSQL statistics."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

        if not container_exists_on_vm(cfg, vm_ip, self.service, root):
            return [VerifyCheck(self.service, "database_scrape", False, "container missing")]
        rc, body = docker_curl(cfg, vm_ip, self.service, "http://localhost:9187/metrics", root=root, timeout=15)
        up = bool(re.search(r"^pg_up(?:\{[^}]*\})?\s+1(?:\.0+)?$", body or "", re.MULTILINE))
        statistics = "pg_stat_database_" in (body or "")
        passed = rc == 0 and up and statistics
        return [
            VerifyCheck(
                self.service,
                "database_scrape",
                passed,
                "pg_up=1 and database statistics exported" if passed else "database scrape is incomplete",
            )
        ]
