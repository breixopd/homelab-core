from __future__ import annotations

import importlib
import stat
from pathlib import Path

import pytest
from toolkit.core.config.config import Config
from toolkit.core.generate.artifacts import (
    ArtifactGenerationContext,
    GeneratedArtifactError,
    generate_service_artifacts,
)
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.services import ServicePlugin, get_service_plugin


def _manifest() -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Generated artifact test service",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": 50,
            "generated_artifacts": [
                {"path": "generated/example.conf", "sensitive": True},
                {"path": "generated/example-health.sh", "executable": True},
                {"path": "generated/example-current", "kind": "symlink"},
            ],
        }
    )


def test_generation_context_writes_only_declared_artifacts_with_safe_modes(tmp_path: Path) -> None:
    context = ArtifactGenerationContext(Config(), tmp_path, {"TOKEN": "secret"}, _manifest())

    context.write_text("generated/example.conf", "token=secret\n")
    context.write_text("generated/example-health.sh", "#!/bin/sh\nexit 0\n")
    context.write_symlink("generated/example-current", "generated/example.conf")
    written = context.finish()

    assert written == (
        tmp_path / "generated/example.conf",
        tmp_path / "generated/example-health.sh",
        tmp_path / "generated/example-current",
    )
    assert stat.S_IMODE((tmp_path / "generated/example.conf").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "generated/example-health.sh").stat().st_mode) == 0o500
    assert (tmp_path / "generated/example-current").read_text() == "token=secret\n"


def test_generation_context_rejects_undeclared_or_incomplete_artifacts(tmp_path: Path) -> None:
    context = ArtifactGenerationContext(Config(), tmp_path, {}, _manifest())

    with pytest.raises(GeneratedArtifactError, match="undeclared artifact"):
        context.write_text("generated/other.conf", "invalid")

    context.write_text("generated/example.conf", "ready")
    with pytest.raises(GeneratedArtifactError, match="did not produce"):
        context.finish()


def test_generation_context_claims_valid_existing_artifact(tmp_path: Path) -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Generated artifact test service",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": 50,
            "generated_artifacts": [{"path": "generated/existing.conf"}],
        }
    )
    path = tmp_path / "generated/existing.conf"
    path.parent.mkdir(parents=True)
    path.write_text("existing")
    context = ArtifactGenerationContext(Config(), tmp_path, {}, manifest)

    context.claim("generated/existing.conf")

    assert context.finish() == (path,)


def test_generation_context_applies_declared_data_owner_when_running_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Generated artifact owner test service",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": 50,
            "host_sources": {"EXAMPLE_CONFIG_SOURCE": {"path": "generated/example"}},
            "generated_artifacts": [{"path": "generated/example/config.yml", "sensitive": True}],
            "stateful": True,
            "data_specs": [
                {
                    "name": "config",
                    "source_env": "EXAMPLE_CONFIG_SOURCE",
                    "target": "/config",
                    "size_estimate_gb": 1,
                    "host_uid": 1000,
                    "host_gid": 1001,
                }
            ],
        }
    )
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr("toolkit.core.generate.artifacts.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "toolkit.core.generate.artifacts.os.chown",
        lambda _path, uid, gid: ownership.append((uid, gid)),
    )
    context = ArtifactGenerationContext(Config(), tmp_path, {}, manifest)

    context.write_text("generated/example/config.yml", "secret: value\n")

    assert ownership == [(1000, 1001)]


def test_generation_context_applies_declared_artifact_owner_and_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Rootless generated secret",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": 50,
            "generated_artifacts": [
                {
                    "path": "generated/example-secret.json",
                    "sensitive": True,
                    "host_uid": 1000,
                    "host_gid": 1000,
                }
            ],
        }
    )
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr("toolkit.core.generate.artifacts.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "toolkit.core.generate.artifacts.os.chown",
        lambda _path, uid, gid: ownership.append((uid, gid)),
    )
    context = ArtifactGenerationContext(Config(), tmp_path, {}, manifest)

    path = context.write_text("generated/example-secret.json", "secret: value\n")

    assert ownership == [(1000, 1000)]
    assert path.stat().st_mode & 0o777 == 0o600


def test_service_artifact_generation_is_priority_ordered_and_complete(tmp_path: Path) -> None:
    calls: list[str] = []
    progress: list[tuple[int, int, str]] = []

    class GeneratorPlugin(ServicePlugin):
        def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
            calls.append(self.service)
            context.write_text(f"generated/{self.service}.conf", f"name={self.service}\n")

    plugins: list[ServicePlugin] = []
    for name, priority in (("later", 80), ("earlier", 10)):
        plugin = GeneratorPlugin()
        plugin._yaml_data = {
            "name": name,
            "label": name.title(),
            "description": f"{name} generated artifact service",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": priority,
            "generated_artifacts": [{"path": f"generated/{name}.conf"}],
        }
        plugin.service = name
        plugins.append(plugin)

    written = generate_service_artifacts(
        Config(),
        tmp_path,
        {"TOKEN": "secret"},
        plugins=plugins,
        on_progress=lambda completed, total, service: progress.append((completed, total, service)),
    )

    assert calls == ["earlier", "later"]
    assert progress == [(1, 2, "earlier"), (2, 2, "later")]
    assert written == [tmp_path / "generated/earlier.conf", tmp_path / "generated/later.conf"]


def test_wazuh_bcrypt_hash_is_reused_until_the_password_changes() -> None:
    module = importlib.import_module("toolkit.services.wazuh-indexer.plugin")

    first = module._stable_bcrypt_hash("initial-password", "")
    unchanged = module._stable_bcrypt_hash("initial-password", first)
    rotated = module._stable_bcrypt_hash("changed-password", first)

    assert unchanged == first
    assert rotated != first


def test_gluetun_artifact_uses_the_manifest_owned_provider_setting(tmp_path: Path) -> None:
    plugin = get_service_plugin("gluetun")
    assert plugin is not None
    config = Config(service_settings={"gluetun": {"provider": "protonvpn"}})
    context = ArtifactGenerationContext(
        config,
        tmp_path,
        {"VPN_PROVIDER": "nordvpn", "VPN_TYPE": "openvpn"},
        plugin.manifest,
    )

    plugin.generate_artifacts(context)
    context.finish()

    generated = (tmp_path / "generated/.env.vpn").read_text()
    assert "VPN_SERVICE_PROVIDER=protonvpn\n" in generated
    assert "VPN_TYPE=openvpn\n" in generated
