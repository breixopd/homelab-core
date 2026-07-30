from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.generate.generate import generate_all, generate_configs
from toolkit.core.generate.validate import validate_generated_artifacts


def test_validate_generated_artifacts_reports_missing_generated(tmp_path: Path):
    save_config(Config(domain="example.com", email="admin@example.com"), config_path(tmp_path))

    report = validate_generated_artifacts(tmp_path)

    assert report.ok is False
    assert any("generated/ directory is missing" in err for err in report.errors)


def test_validate_generated_artifacts_succeeds_without_optional_tooling(tmp_path: Path, monkeypatch):
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(tmp_path))
    generate_all(tmp_path)
    generate_configs(cfg, tmp_path)

    monkeypatch.setattr("toolkit.core.generate.validate.shutil.which", lambda name: None)

    report = validate_generated_artifacts(tmp_path)

    assert report.ok is True
    assert any("Caddyfile present" in check for check in report.checks)
    assert any("docker compose validation skipped" in skipped for skipped in report.skipped)
