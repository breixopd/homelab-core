import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest
from toolkit.core.config.config import Config
from toolkit.core.deploy.deploy_workflow import _run_pre_deploy_dump
from toolkit.core.ops.database_provider import DatabaseProviderDisabledError


def _provider(plugin: object) -> SimpleNamespace:
    return SimpleNamespace(manifest=SimpleNamespace(name="postgres"), plugin=plugin)


def test_pre_deploy_dump_success_returns_artifact(monkeypatch, tmp_path: Path) -> None:
    calls: list[str | None] = []

    class Plugin:
        def pre_deploy_database_dump(self, cfg, root, *, vm=None):
            calls.append(vm)
            path = root / "generated" / "pre-deploy.sql.gz"
            path.parent.mkdir()
            with gzip.open(path, "wb") as stream:
                stream.write(b"SELECT 1;\n")
            return str(path)

    provider = _provider(Plugin())
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", lambda cfg: provider)
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_node", lambda cfg, p: "control")

    applicable, path = _run_pre_deploy_dump(Config(proxmox={"provision_machines": False}), tmp_path)

    assert applicable is True
    assert path == str(tmp_path / "generated" / "pre-deploy.sql.gz")
    assert calls == ["control"]


def test_pre_deploy_dump_failure_is_hard_gate(monkeypatch, tmp_path: Path) -> None:
    class Plugin:
        def pre_deploy_database_dump(self, cfg, root, *, vm=None):
            raise OSError("database unreachable")

    provider = _provider(Plugin())
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", lambda cfg: provider)
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_node", lambda cfg, p: "control")

    with pytest.raises(RuntimeError, match="pre-deploy dump failed"):
        _run_pre_deploy_dump(Config(proxmox={"provision_machines": False}), tmp_path)


def test_pre_deploy_dump_empty_result_is_hard_gate(monkeypatch, tmp_path: Path) -> None:
    class Plugin:
        def pre_deploy_database_dump(self, cfg, root, *, vm=None):
            return None

    provider = _provider(Plugin())
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", lambda cfg: provider)
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_node", lambda cfg, p: "control")

    with pytest.raises(RuntimeError, match="produced no artifact"):
        _run_pre_deploy_dump(Config(proxmox={"provision_machines": False}), tmp_path)


def test_pre_deploy_dump_missing_local_artifact_is_hard_gate(monkeypatch, tmp_path: Path) -> None:
    class Plugin:
        def pre_deploy_database_dump(self, cfg, root, *, vm=None):
            return str(root / "generated" / "missing.sql.gz")

    provider = _provider(Plugin())
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", lambda cfg: provider)
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_node", lambda cfg, p: "control")

    with pytest.raises(RuntimeError, match="missing, empty, or invalid"):
        _run_pre_deploy_dump(Config(proxmox={"provision_machines": False}), tmp_path)


def test_pre_deploy_dump_truncated_local_gzip_is_hard_gate(monkeypatch, tmp_path: Path) -> None:
    class Plugin:
        def pre_deploy_database_dump(self, cfg, root, *, vm=None):
            path = root / "generated" / "truncated.sql.gz"
            path.parent.mkdir()
            with gzip.open(path, "wb") as stream:
                stream.write(b"SELECT 1;\n")
            path.write_bytes(path.read_bytes()[:-4])
            return str(path)

    provider = _provider(Plugin())
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", lambda cfg: provider)
    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_node", lambda cfg, p: "control")

    with pytest.raises(RuntimeError, match="missing, empty, or invalid"):
        _run_pre_deploy_dump(Config(proxmox={"provision_machines": False}), tmp_path)


@pytest.mark.parametrize(
    "error",
    [DatabaseProviderDisabledError("primary database provider 'postgres' is disabled"), KeyError("primary-database")],
)
def test_pre_deploy_dump_skips_when_provider_not_applicable(monkeypatch, tmp_path: Path, error: Exception) -> None:
    def resolve_provider(cfg):
        raise error

    monkeypatch.setattr("toolkit.core.ops.database_provider.primary_database_provider", resolve_provider)

    applicable, path = _run_pre_deploy_dump(Config(), tmp_path)

    assert applicable is False
    assert path is None
