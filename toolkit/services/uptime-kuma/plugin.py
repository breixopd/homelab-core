"""Uptime Kuma service plugin — status page + uptime monitoring.

Auto-creates the admin account + registers HTTP monitors for every public
service route on first boot. Replaces the clunky static HTML health report.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class UptimeKumaPlugin(ServicePlugin):
    service = "uptime-kuma"
    category = "notifications"

    def post_start(self, cfg: Config, secrets: dict, *, root: Path | None = None) -> list[str]:
        """Create the admin account + register HTTP monitors for all public services."""
        import importlib

        return importlib.import_module("toolkit.services.uptime-kuma.bootstrap").bootstrap_uptime_kuma(cfg, secrets)

    def verify(self, cfg: Config, secrets: dict, vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify status page HTTP + SQLite monitor count (proves DB is working)."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl, docker_exec_on_vm

        if cfg.domain == "localhost":
            return [VerifyCheck("uptime-kuma", "status-page-http", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "uptime-kuma", root):
            return [VerifyCheck("uptime-kuma", "status-page-http", False, "container missing")]

        checks: list[VerifyCheck] = []
        rc, _body = docker_curl(cfg, vm_ip, "uptime-kuma", "http://localhost:3001/", root=root, timeout=10)
        http_ok = rc == 0
        checks.append(
            VerifyCheck(
                service="uptime-kuma",
                check="status-page-http",
                passed=http_ok,
                detail="HTTP ok" if http_ok else "unreachable",
            )
        )

        rc, out = docker_exec_on_vm(
            cfg,
            "uptime-kuma",
            [
                "node",
                "-e",
                (
                    "const sqlite3=require('@louislam/sqlite3');"
                    "const db=new sqlite3.Database('/app/data/kuma.db',sqlite3.OPEN_READONLY,(e)=>{"
                    "if(e){console.error(e.message);process.exit(1)}});"
                    "db.get('SELECT COUNT(*) AS count FROM monitor',(e,row)=>{"
                    "if(e){console.error(e.message);process.exitCode=1}"
                    "else{console.log(row.count)}db.close()})"
                ),
            ],
            vm_ip,
            root,
            timeout=15,
        )
        if rc == 0 and (out or "").strip().isdigit():
            count = int((out or "0").strip())
        else:
            count = -1

        if count == 0:
            try:
                import importlib

                bootstrap = importlib.import_module("toolkit.services.uptime-kuma.bootstrap")
                bootstrap.bootstrap_uptime_kuma(cfg, secrets)
                rc2, out2 = docker_exec_on_vm(
                    cfg,
                    "uptime-kuma",
                    [
                        "node",
                        "-e",
                        (
                            "const sqlite3=require('@louislam/sqlite3');"
                            "const db=new sqlite3.Database('/app/data/kuma.db',sqlite3.OPEN_READONLY,(e)=>{"
                            "if(e){console.error(e.message);process.exit(1)}});"
                            "db.get('SELECT COUNT(*) AS count FROM monitor',(e,row)=>{"
                            "if(e){console.error(e.message);process.exitCode=1}"
                            "else{console.log(row.count)}db.close()})"
                        ),
                    ],
                    vm_ip,
                    root,
                    timeout=15,
                )
                if rc2 == 0 and (out2 or "").strip().isdigit():
                    count = int((out2 or "0").strip())
            except Exception:
                pass

        if count > 0:
            checks.append(
                VerifyCheck(
                    "uptime-kuma",
                    "monitors",
                    True,
                    f"{count} monitor(s) in DB",
                )
            )
        elif count == 0:
            checks.append(
                VerifyCheck(
                    "uptime-kuma",
                    "monitors",
                    False,
                    "no monitors registered (status page up; bootstrap could not register — check admin password)",
                )
            )
        else:
            checks.append(
                VerifyCheck(
                    "uptime-kuma",
                    "monitors",
                    False,
                    "monitor database probe failed",
                )
            )

        return checks
