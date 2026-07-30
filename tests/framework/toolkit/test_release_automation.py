from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_release_automation_has_one_version_source_and_valid_bootstrap_state() -> None:
    config = json.loads((ROOT / ".github/release-please-config.json").read_text())
    manifest = json.loads((ROOT / ".github/.release-please-manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package = config["packages"]["."]

    assert config["$schema"].endswith("/release-please/v17.6.0/schemas/config.json")
    assert re.fullmatch(r"[0-9a-f]{40}", config["bootstrap-sha"])
    assert package["release-type"] == "python"
    assert package["include-component-in-tag"] is False
    assert package["include-v-in-tag"] is True
    assert "versioning" not in package
    assert manifest == {".": project["project"]["version"]}
    assert "__version__" not in (ROOT / "toolkit/__init__.py").read_text()


def test_release_workflow_uses_the_versioned_manifest_configuration() -> None:
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text()

    assert "config-file: .github/release-please-config.json" in workflow
    assert "manifest-file: .github/.release-please-manifest.json" in workflow
