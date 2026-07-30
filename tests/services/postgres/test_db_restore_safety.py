from __future__ import annotations

import gzip
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner
from tests.helpers.machines import machines_with_addresses, renamed_default_machines
from toolkit.cli.maintenance_cmd import maintenance
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.ops.db_safety import list_dumps, pre_deploy_dump, restore_dump
from toolkit.core.ops.dump_repository import DumpRecord, DumpRepository
from toolkit.services import ServicePlugin


def _remote_cfg():
    return Config(machines=machines_with_addresses(infra="10.0.0.2"))


def _remote_record() -> DumpRecord:
    path = "/opt/homelab/generated/pre-deploy-dumps/pre-deploy-20260709-120000.sql.gz"
    return DumpRepository.remote(
        "/opt/homelab/generated/pre-deploy-dumps",
        [{"path": path, "size_bytes": 12, "sha256": "a" * 64}],
    ).list()[0]


def test_local_predeploy_dump_is_controller_private(tmp_path: Path) -> None:
    from toolkit.services.postgres.maintenance import _local_dump, _PostgresContract

    contract = _PostgresContract("postgres", "postgres", "admin", "postgres")

    def write_dump(*_args, **kwargs):
        kwargs["stdout"].write(b"CREATE DATABASE postgres;\n")
        return SimpleNamespace(returncode=0)

    with patch("toolkit.services.postgres.maintenance.subprocess.run", side_effect=write_dump):
        path = _local_dump(tmp_path, contract)

    assert path is not None
    dump = Path(path)
    assert stat.S_IMODE(dump.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600


def test_local_predeploy_dump_rejects_empty_gzip(tmp_path: Path) -> None:
    from toolkit.services.postgres.maintenance import _local_dump, _PostgresContract

    contract = _PostgresContract("postgres", "postgres", "admin", "postgres")
    with patch(
        "toolkit.services.postgres.maintenance.subprocess.run",
        return_value=SimpleNamespace(returncode=0),
    ):
        assert _local_dump(tmp_path, contract) is None


def test_local_predeploy_dump_rejects_nonexistent_artifact() -> None:
    from toolkit.services.postgres.maintenance import _valid_dump_file

    assert not _valid_dump_file(Path("/tmp/does-not-exist-homelab-dump.sql.gz"))


def test_local_predeploy_dump_rejects_truncated_gzip(tmp_path: Path) -> None:
    from toolkit.services.postgres.maintenance import _valid_dump_file

    path = tmp_path / "truncated.sql.gz"
    with gzip.open(path, "wb") as stream:
        stream.write(b"SELECT 1;\n")
    path.write_bytes(path.read_bytes()[:-4])

    assert not _valid_dump_file(path)


class _PrimaryDatabasePlugin(ServicePlugin):
    service = "custom-database"

    def pre_deploy_database_dump(self, cfg: Config, root: Path, *, vm: str | None = None) -> str | None:
        return f"{self.service}:{vm}"

    def list_database_dumps(self, cfg: Config, root: Path, *, vm: str | None = None) -> list[DumpRecord]:
        return []

    def restore_database_dump(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> bool:
        return True

    def run_database_restore_drill(
        self,
        cfg: Config,
        root: Path,
        record: DumpRecord,
        *,
        vm: str | None = None,
    ) -> tuple[bool, int, str]:
        return True, 1, "ok"


def test_database_recovery_dispatches_through_primary_database_capability(tmp_path: Path) -> None:
    postgres = load_service_catalog().require("postgres")
    custom_database = postgres.model_copy(
        update={
            "name": "custom-database",
            "provides": ("primary-database",),
            "backup_exports": (),
        }
    )
    catalog = ServiceCatalog((custom_database,))
    plugin = _PrimaryDatabasePlugin()
    cfg = _remote_cfg()

    with (
        patch("toolkit.core.manifest.catalog.load_service_catalog", return_value=catalog),
        patch("toolkit.services.get_service_plugin", return_value=plugin),
    ):
        result = pre_deploy_dump(cfg, tmp_path)

    assert result == "custom-database:infra"


def test_remote_discovery_discards_shell_metacharacter_filename(tmp_path: Path) -> None:
    valid = _remote_record()
    output = (
        f"{valid.path}\t12\t{'a' * 64}\n"
        "/opt/homelab/generated/pre-deploy-dumps/pre-deploy-20260709-120000.sql.gz;id\t12\t"
        f"{'b' * 64}\n"
    )
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, output, ""),
    ):
        records = list_dumps(_remote_cfg(), tmp_path)

    assert records == [valid]


def test_database_dump_uses_configured_postgres_admin_role(tmp_path: Path) -> None:
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "OK", ""),
    ) as ssh:
        assert pre_deploy_dump(_remote_cfg(), tmp_path)

    assert "pg_dumpall -U admin" in ssh.call_args.args[2]


def test_remote_dump_fails_when_pg_dumpall_fails_even_if_gzip_completes(tmp_path: Path) -> None:
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(1, "", "pg_dumpall failed"),
    ) as ssh:
        assert pre_deploy_dump(_remote_cfg(), tmp_path) is None

    command = ssh.call_args.args[2]
    assert "bash -o pipefail -c" in command
    assert "test -s" in command
    assert "gzip -t" in command


def test_remote_dump_success_requires_validated_artifact_command(tmp_path: Path) -> None:
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "OK", ""),
    ) as ssh:
        assert pre_deploy_dump(_remote_cfg(), tmp_path)

    command = ssh.call_args.args[2]
    assert "gzip -cd" in command
    assert "wc -c" in command


def test_database_operations_follow_postgres_manifest_placement(tmp_path: Path) -> None:
    cfg = Config(machines=renamed_default_machines())
    record = _remote_record()
    discovery = f"{record.path}\t12\t{'a' * 64}\n"
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        side_effect=[(0, "OK", ""), (0, discovery, ""), (0, "ok", "")],
    ) as ssh:
        assert pre_deploy_dump(cfg, tmp_path)
        assert list_dumps(cfg, tmp_path) == [record]
        assert restore_dump(cfg, tmp_path, record, actor="test")

    assert [call.args[1] for call in ssh.call_args_list] == [cfg.node_ip("core")] * 3
    restore_audit = (tmp_path / ".homelab-state" / "audit.log").read_text()
    assert '"vm":"core"' in restore_audit


def test_monolithic_dump_discovery_uses_local_repository(tmp_path: Path) -> None:
    dump_dir = tmp_path / "generated" / "pre-deploy-dumps"
    dump_dir.mkdir(parents=True)
    with gzip.open(dump_dir / "pre-deploy-20260709-120000.sql.gz", "wb") as stream:
        stream.write(b"select 1;")

    records = list_dumps(Config(proxmox={"provision_machines": False}), tmp_path)

    assert len(records) == 1
    assert records[0].is_remote is False


def test_remote_restore_verifies_hash_and_records_audit(tmp_path: Path) -> None:
    record = _remote_record()
    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, "ok", ""),
    ) as ssh:
        assert restore_dump(_remote_cfg(), tmp_path, record, actor="test")

    command = ssh.call_args.args[2]
    assert "sha256sum" in command
    assert "bash -o pipefail" in command
    assert record.sha256 in command
    assert record.path in command
    assert "psql -v ON_ERROR_STOP=1 -U admin -d postgres" in command
    assert list((tmp_path / ".homelab-state" / "restore-intents").glob("*.json"))
    audit = (tmp_path / ".homelab-state" / "audit.log").read_text()
    assert '"action":"restore"' in audit
    assert record.dump_id in audit


def test_cli_restore_requires_exact_discovered_id_confirmation(tmp_path: Path) -> None:
    record = _remote_record()
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, _remote_cfg())),
        patch("toolkit.core.ops.db_safety.list_dumps", return_value=[record]),
        patch("toolkit.core.ops.db_safety.restore_dump") as restore,
    ):
        result = CliRunner().invoke(
            maintenance,
            ["restore-db", record.dump_id, "--confirm-dump-id", "wrong"],
        )

    assert result.exit_code != 0
    assert "confirmation did not match" in result.output
    restore.assert_not_called()


def test_cli_restore_passes_only_repository_record(tmp_path: Path) -> None:
    record = _remote_record()
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, _remote_cfg())),
        patch("toolkit.core.ops.db_safety.list_dumps", return_value=[record]),
        patch("toolkit.core.ops.db_safety.restore_dump", return_value=True) as restore,
    ):
        result = CliRunner().invoke(
            maintenance,
            ["restore-db", record.dump_id, "--confirm-dump-id", record.dump_id],
        )

    assert result.exit_code == 0, result.output
    assert restore.call_args.args[2] is record
    assert restore.call_args.kwargs == {"actor": "cli"}
