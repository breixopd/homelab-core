"""Unit tests for Recyclarr v8 config generation."""

from __future__ import annotations

import re
from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.core.generate.artifacts import ArtifactGenerationContext
from toolkit.services import get_service_plugin
from toolkit.services.recyclarr.plugin import generate_recyclarr_config


def _generate(root: Path, sonarr: str = "", radarr: str = "") -> tuple[Path, ...]:
    plugin = get_service_plugin("recyclarr")
    assert plugin is not None
    context = ArtifactGenerationContext(
        Config(domain="test.local"),
        root,
        {"SONARR_API_KEY": sonarr, "RADARR_API_KEY": radarr},
        plugin.manifest,
    )
    generate_recyclarr_config(context)
    return context.finish()


def test_generate_recyclarr_config_writes_v8_schema(tmp_path):
    written = _generate(tmp_path, "sonarr-test-key", "radarr-test-key")
    assert len(written) == 4

    config = (tmp_path / "generated/recyclarr/recyclarr.yml").read_text()
    settings = (tmp_path / "generated/recyclarr/settings.yml").read_text()

    assert "yaml-language-server: $schema=https://schemas.recyclarr.dev/latest/config-schema.json" in config
    assert "resource_providers:" in settings
    assert "type: trash-guides" in settings
    assert "type: config-templates" in settings
    assert "reference: v8" in settings

    assert "assign_scores_to:" in config
    assert "quality_profiles:" in config
    assert "trash_id:" in config
    assert "quality_definition:" in config
    assert "type: series" in config
    assert "type: movie" in config
    assert "sonarr-test-key" in config
    assert "radarr-test-key" in config
    assert "http://sonarr:8989" in config
    assert "http://radarr:7878" in config

    assert "repositories:" not in config
    assert "repositories:" not in settings
    assert "release_profiles:" not in config
    assert "quality_definition: hybrid" not in config
    assert "quality_definition: movie" not in config

    nested_cf_profiles = re.search(
        r"custom_formats:.*?quality_profiles:",
        config,
        flags=re.DOTALL,
    )
    assert nested_cf_profiles is None, "custom_formats must use assign_scores_to, not quality_profiles"


def test_generate_recyclarr_config_idempotent(tmp_path):
    _generate(tmp_path, "first-sonarr-key", "first-radarr-key")
    config_path = tmp_path / "generated/recyclarr/recyclarr.yml"
    initial_mtime = config_path.stat().st_mtime_ns
    _generate(tmp_path, "first-sonarr-key", "first-radarr-key")
    assert config_path.stat().st_mtime_ns == initial_mtime
    _generate(tmp_path, "second-sonarr-key", "second-radarr-key")
    config = config_path.read_text()
    assert "second-sonarr-key" in config
    assert "second-radarr-key" in config
    assert "include:" in config
    assert "config: sonarr-local.yml" in config
    assert "config: radarr-local.yml" in config
    assert (tmp_path / "generated/recyclarr/includes/sonarr-local.yml").is_file()
    assert (tmp_path / "generated/recyclarr/includes/radarr-local.yml").is_file()


def test_generate_recyclarr_config_env_placeholders(tmp_path):
    _generate(tmp_path)
    config = (tmp_path / "generated/recyclarr/recyclarr.yml").read_text()
    assert "${SONARR_API_KEY}" in config
    assert "${RADARR_API_KEY}" in config


def test_generate_recyclarr_config_guide_profile_trash_ids(tmp_path):
    _generate(tmp_path, "s", "r")
    config = (tmp_path / "generated/recyclarr/recyclarr.yml").read_text()

    assert "72dae194fc92bf828f32cde7744e51a1" in config  # Sonarr WEB-1080p
    assert "d1d67249d3890e49bc12e275d989a7e9" in config  # Radarr HD Bluray + WEB
    assert "64fb5f9858489bdac2af690e27c8f42f" in config  # Radarr UHD Bluray + WEB
    assert "name: HD-1080p" in config
    assert "name: UHD" in config
