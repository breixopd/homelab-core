from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from toolkit.core.config.config import Config
from toolkit.core.generate.compose_assemble import (
    apply_manifest_release_versions,
    apply_release_images,
    assemble_compose_text,
)


def test_repository_compose_is_generated_only_from_service_apps_and_platform() -> None:
    root = Path.cwd()
    generated = yaml.safe_load(assemble_compose_text(root, Config(), include_release=False))
    tracked = yaml.safe_load((root / "docker-compose.example.yml").read_text(encoding="utf-8"))

    assert generated == tracked
    assert {path.name for path in (root / "stacks").iterdir()} == {"README.md", "platform.yaml"}


def test_release_images_replace_only_declared_compose_services() -> None:
    document = {"services": {"redis": {"image": "redis:8"}, "local": {"build": "."}}}
    digest = "docker.io/library/redis@sha256:" + ("a" * 64)

    apply_release_images(document, {"redis": digest})

    assert document["services"]["redis"]["image"] == digest
    assert document["services"]["local"] == {"build": "."}


def test_release_images_reject_unknown_or_image_less_services() -> None:
    document = {"services": {"local": {"build": "."}}}
    digest = "docker.io/library/example@sha256:" + ("a" * 64)

    with pytest.raises(ValueError, match="unknown or image-less"):
        apply_release_images(document, {"missing": digest, "local": digest})


def test_manifest_release_versions_project_mutable_tags_for_scanning(monkeypatch, tmp_path: Path) -> None:
    release = SimpleNamespace(compose_service="example", version_ref="ghcr.io/example/example:v1.2.3")
    catalog = SimpleNamespace(manifests=(SimpleNamespace(image_release=release),))
    monkeypatch.setattr("toolkit.core.manifest.catalog.load_service_catalog", lambda _root: catalog)
    monkeypatch.setattr("toolkit.core.manifest.routes.service_is_enabled", lambda *_args: True)
    document = {
        "services": {
            "example": {
                "image": "ghcr.io/example/example:v1.2.3@sha256:" + ("a" * 64),
            }
        }
    }

    apply_manifest_release_versions(tmp_path, Config(), document)

    assert document["services"]["example"]["image"] == "ghcr.io/example/example:v1.2.3"


def test_manifest_release_versions_skip_disabled_services(monkeypatch, tmp_path: Path) -> None:
    release = SimpleNamespace(compose_service="disabled", version_ref="ghcr.io/example/disabled:v1")
    catalog = SimpleNamespace(manifests=(SimpleNamespace(image_release=release),))
    monkeypatch.setattr("toolkit.core.manifest.catalog.load_service_catalog", lambda _root: catalog)
    monkeypatch.setattr("toolkit.core.manifest.routes.service_is_enabled", lambda *_args: False)
    document = {"services": {}}

    apply_manifest_release_versions(tmp_path, Config(), document)

    assert document == {"services": {}}
