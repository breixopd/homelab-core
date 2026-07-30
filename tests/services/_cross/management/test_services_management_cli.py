from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.controller.contracts import ConfigApplyOperation, ServiceActionOperation
from toolkit.controller.service_management_api import read_service_management, update_service_settings
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


class _Controller:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.operations: list[object] = []

    def close(self) -> None:
        pass

    def service_management(self, service: str):
        return read_service_management(self.root, service, collect_status=False)

    def update_service_settings(self, service: str, update):
        return update_service_settings(self.root, service, update)

    def submit(self, request):
        self.operations.append(request.operation)
        return SimpleNamespace(job_id=f"job-{len(self.operations)}")


def _invoke(tmp_path: Path, monkeypatch, arguments: list[str]):
    save_config(Config(domain="example.com"), config_path(tmp_path))
    controller = _Controller(tmp_path)
    monkeypatch.setattr("toolkit.cli.controller_client_from_environment", lambda: controller)
    result = CliRunner().invoke(main, ["--root", str(tmp_path), "services", *arguments])
    return result, controller


def test_services_inspect_renders_plugin_declared_management(tmp_path: Path, monkeypatch) -> None:
    result, _controller = _invoke(tmp_path, monkeypatch, ["inspect", "music-sync"])

    assert result.exit_code == 0, result.exception
    assert "Music Sync" in result.output
    assert "Sync interval" in result.output
    assert "Sync now" in result.output
    assert "Imported tracks" in result.output


def test_services_set_validates_and_queues_reconciliation(tmp_path: Path, monkeypatch) -> None:
    result, controller = _invoke(tmp_path, monkeypatch, ["set", "music-sync", "interval-minutes", "30"])

    assert result.exit_code == 0, result.exception
    assert "Saved interval-minutes=30" in result.output
    assert "job-1" in result.output
    assert len(controller.operations) == 1
    assert isinstance(controller.operations[0], ConfigApplyOperation)
    values = {setting.key: setting.value for setting in controller.service_management("music-sync").settings}
    assert values["interval-minutes"] == 30


def test_services_deploy_queues_durable_service_reconciliation(tmp_path: Path, monkeypatch) -> None:
    result, controller = _invoke(tmp_path, monkeypatch, ["deploy", "music-sync"])

    assert result.exit_code == 0, result.exception
    assert "Music Sync reconciliation queued" in result.output
    assert "job-1" in result.output
    assert len(controller.operations) == 1
    operation = controller.operations[0]
    assert isinstance(operation, ConfigApplyOperation)
    assert operation.service == "music-sync"


def test_services_set_rejects_invalid_boolean(tmp_path: Path, monkeypatch) -> None:
    result, controller = _invoke(tmp_path, monkeypatch, ["set", "music-sync", "enabled", "sometimes"])

    assert result.exit_code != 0
    assert "true or false" in result.output
    assert controller.operations == []


def test_services_run_queues_plugin_declared_action(tmp_path: Path, monkeypatch) -> None:
    result, controller = _invoke(tmp_path, monkeypatch, ["run", "music-sync", "sync-now", "--yes"])

    assert result.exit_code == 0, result.exception
    assert "Sync now queued as job job-1" in result.output
    assert len(controller.operations) == 1
    operation = controller.operations[0]
    assert isinstance(operation, ServiceActionOperation)
    assert operation.service == "music-sync"
    assert operation.action == "sync-now"
