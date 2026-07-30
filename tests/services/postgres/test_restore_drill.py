from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from tests.helpers.machines import machines_with_addresses, renamed_default_machines
from toolkit.cli.maintenance_cmd import maintenance
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.ops.dump_repository import DumpRepository
from toolkit.core.ops.restore_drill import RestoreDrillResult, run_restore_drill
from toolkit.services import ServicePlugin


def _cfg():
    return Config(machines=machines_with_addresses(infra="10.0.0.2"))


def _record():
    path = "/opt/homelab/generated/pre-deploy-dumps/pre-deploy-20260709-120000.sql.gz"
    return DumpRepository.remote(
        "/opt/homelab/generated/pre-deploy-dumps",
        [{"path": path, "size_bytes": 42, "sha256": "a" * 64}],
    ).list()[0]


class _PrimaryDatabaseDrillPlugin(ServicePlugin):
    service = "custom-database"

    def pre_deploy_database_dump(self, cfg, root, *, vm=None) -> str | None:
        return None

    def list_database_dumps(self, cfg, root, *, vm=None):
        return []

    def restore_database_dump(self, cfg, root, record, *, vm=None) -> bool:
        return True

    def run_database_restore_drill(self, cfg, root, record, *, vm=None) -> tuple[bool, int, str]:
        return True, 4, "ok"


def test_restore_drill_dispatches_through_primary_database_capability(tmp_path: Path) -> None:
    postgres = load_service_catalog().require("postgres")
    custom_database = postgres.model_copy(
        update={
            "name": "custom-database",
            "provides": ("primary-database",),
            "backup_exports": (),
        }
    )
    catalog = ServiceCatalog((custom_database,))

    with (
        patch("toolkit.core.manifest.catalog.load_service_catalog", return_value=catalog),
        patch("toolkit.services.get_service_plugin", return_value=_PrimaryDatabaseDrillPlugin()),
    ):
        result = run_restore_drill(_cfg(), tmp_path, _record(), actor="test")

    assert result.ok is True
    assert result.database_count == 4


def test_successful_remote_restore_drill_records_verified_checkpoint(tmp_path: Path) -> None:
    record = _record()
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "DATABASE_COUNT=8\n", ""),
    ) as ssh:
        result = run_restore_drill(_cfg(), tmp_path, record, actor="test")

    assert result.ok is True
    assert result.database_count == 8
    command = ssh.call_args.args[2]
    assert "docker inspect" in command
    assert "ON_ERROR_STOP=1" in command
    assert "SELECT 1" in command
    assert "pg_isready" not in command
    assert "trap cleanup EXIT" in command
    assert record.sha256 in command
    assert record.path in command
    checkpoint = json.loads((tmp_path / ".homelab-state" / "checkpoints" / "latest.json").read_text())
    assert checkpoint["scope"] == ["apps", "infra", "media"]
    evidence_path = Path(next(iter(checkpoint["evidence"])))
    assert evidence_path.is_file()
    assert evidence_path.stat().st_mode & 0o777 == 0o600


def test_remote_restore_drill_follows_postgres_manifest_placement(tmp_path: Path) -> None:
    cfg = Config(machines=renamed_default_machines())
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "DATABASE_COUNT=3\n", ""),
    ) as ssh:
        result = run_restore_drill(cfg, tmp_path, _record(), actor="test")

    assert result.ok
    assert ssh.call_args.args[1] == cfg.node_ip("core")
    evidence = json.loads(result.evidence_path.read_text())
    assert evidence["vm"] == "core"


def test_failed_restore_drill_never_records_checkpoint(tmp_path: Path) -> None:
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(1, "", "restore failed"),
    ):
        result = run_restore_drill(_cfg(), tmp_path, _record(), actor="test")

    assert result.ok is False
    assert not (tmp_path / ".homelab-state" / "checkpoints" / "latest.json").exists()


def test_failed_restore_drill_reports_bounded_stage_error(tmp_path: Path) -> None:
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(1, "", "DRILL_ERROR=target database readiness timed out\n"),
    ):
        result = run_restore_drill(_cfg(), tmp_path, _record(), actor="test")

    assert result.ok is False
    assert result.message == "target database readiness timed out"


def test_restore_drill_rejects_dump_location_mode_mismatch(tmp_path: Path) -> None:
    local_cfg = Config(proxmox={"provision_machines": False})

    result = run_restore_drill(local_cfg, tmp_path, _record(), actor="test")

    assert result.ok is False
    assert "location" in result.message


def test_monolithic_restore_checkpoint_only_covers_deployed_infra_role(tmp_path: Path) -> None:
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    dump_dir.mkdir(parents=True)
    path = dump_dir / "pre-deploy-20260709-120000.sql.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(b"select 1;")
    record = DumpRepository.local(dump_dir).list()[0]
    cfg = Config(proxmox={"provision_machines": False})

    with patch("toolkit.services.postgres.maintenance._local_restore_drill", return_value=(True, 3, "")):
        result = run_restore_drill(cfg, tmp_path, record, actor="test")

    assert result.ok
    checkpoint = json.loads((tmp_path / ".homelab-state" / "checkpoints" / "latest.json").read_text())
    assert checkpoint["scope"] == ["infra"]


def test_restore_drill_cli_runs_only_discovered_dump(tmp_path: Path) -> None:
    record = _record()
    verified = RestoreDrillResult(
        True,
        "isolated restore verified 8 connectable database(s)",
        database_count=8,
        checkpoint_id="checkpoint-1",
    )
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, _cfg())),
        patch("toolkit.core.ops.db_safety.list_dumps", return_value=[record]),
        patch("toolkit.core.ops.restore_drill.run_restore_drill", return_value=verified) as drill,
    ):
        result = CliRunner().invoke(maintenance, ["restore-drill", record.dump_id])

    assert result.exit_code == 0, result.output
    assert "checkpoint-1" in result.output
    assert drill.call_args.args[2] is record


def test_restore_drill_cli_rejects_unknown_id(tmp_path: Path) -> None:
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, _cfg())),
        patch("toolkit.core.ops.db_safety.list_dumps", return_value=[_record()]),
        patch("toolkit.core.ops.restore_drill.run_restore_drill") as drill,
    ):
        result = CliRunner().invoke(maintenance, ["restore-drill", "dmp_unknown"])

    assert result.exit_code != 0
    assert "unknown" in result.output
    drill.assert_not_called()
