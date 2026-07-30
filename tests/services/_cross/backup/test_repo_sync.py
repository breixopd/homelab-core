from __future__ import annotations

import re
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.machines import machines_with_addresses
from toolkit.core.config.config import Config
from toolkit.core.deploy.repo_sync import (
    DEFAULT_SYNC_PATHS,
    _build_tarball,
    sync_repo_to_guest,
    sync_repo_to_role,
)

ROOT = Path(__file__).resolve().parents[4]


def _link_service_catalog(root: Path) -> None:
    services = root / "toolkit" / "services"
    services.parent.mkdir(parents=True, exist_ok=True)
    services.symlink_to(ROOT / "toolkit" / "services", target_is_directory=True)
    (root / "toolkit" / "Dockerfile").symlink_to(ROOT / "toolkit" / "Dockerfile")


def test_build_tarball_includes_existing_paths(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "config.yaml").write_text("domain: example.com\n")
    (root / "toolkit").mkdir()
    (root / "toolkit" / "marker.txt").write_text("sync-me\n")

    archive = _build_tarball(root, ("config.yaml", "toolkit", "missing-dir"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert "config.yaml" in names
        assert "toolkit/marker.txt" in names
        assert "missing-dir" not in names
    finally:
        archive.unlink(missing_ok=True)
        archive.parent.rmdir()


def test_sync_repo_to_guest_success(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _link_service_catalog(root)
    (root / "config.yaml").write_text("domain: example.com\n")
    runtime = root / "generated" / "infra" / ".env"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("RUNTIME=infra\n")
    bundle = root / "generated" / "bundles" / "infra" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("INFRA_ONLY=value\n")
    machines = machines_with_addresses(infra="10.0.0.1")
    machines = {name: spec.model_copy(update={"enabled": name == "infra"}) for name, spec in machines.items()}
    cfg = Config(domain="example.com", machines=machines)

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm") as mock_scp,
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.1", paths=("config.yaml",))

    mock_scp.assert_called_once()
    assert mock_ssh.call_count == 2
    remote_archive = mock_scp.call_args.args[4]
    assert re.fullmatch(r"/root/\.homelab-sync-[0-9a-f]{24}\.tgz", remote_archive)
    cmd = mock_ssh.call_args_list[0].args[2]
    assert "tar xzf" in cmd
    assert remote_archive in cmd


def test_sync_repo_to_guest_extract_failure(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    _link_service_catalog(root)
    (root / "config.yaml").write_text("domain: example.com\n")
    machines = machines_with_addresses(infra="10.0.0.2")
    machines = {name: spec.model_copy(update={"enabled": name == "infra"}) for name, spec in machines.items()}
    cfg = Config(domain="example.com", machines=machines)
    runtime = root / "generated" / "infra" / ".env"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("RUNTIME=infra\n")
    bundle = root / "generated" / "bundles" / "infra" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("INFRA_ONLY=value\n")

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(1, "", "extract boom")),
    ):
        with pytest.raises(RuntimeError, match="extract boom"):
            sync_repo_to_guest(root, cfg, "10.0.0.2")


def test_sync_repo_to_role_unknown_vm(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    from toolkit.core.config.config import save_config
    from toolkit.core.config.storage import config_path

    cfg = Config(domain="example.com")
    save_config(cfg, config_path(root))

    with pytest.raises(ValueError, match="Unknown or disabled machine"):
        sync_repo_to_role(root, "nonexistent")


def test_sync_repo_to_role_delegates(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    from toolkit.core.config.config import save_config
    from toolkit.core.config.storage import config_path

    cfg = Config(domain="example.com", machines=machines_with_addresses(media="10.0.0.5"))
    save_config(cfg, config_path(root))

    with patch("toolkit.core.deploy.repo_sync.sync_repo_to_guest") as mock_sync:
        sync_repo_to_role(root, "media", repo_dest="/opt/homelab")

    mock_sync.assert_called_once()
    args = mock_sync.call_args
    assert args[0][2] == "10.0.0.5"
    assert args[1]["repo_dest"] == "/opt/homelab"


def test_default_sync_paths_cover_repo_tree():
    assert ".dockerignore" in DEFAULT_SYNC_PATHS
    assert "toolkit" in DEFAULT_SYNC_PATHS
    assert "automation" in DEFAULT_SYNC_PATHS
    assert "infrastructure" in DEFAULT_SYNC_PATHS
    assert "stacks" in DEFAULT_SYNC_PATHS
    assert "pyproject.toml" in DEFAULT_SYNC_PATHS
    assert "uv.lock" in DEFAULT_SYNC_PATHS
    assert "docker-compose.yml" in DEFAULT_SYNC_PATHS
    assert ".homelab-state/trust/proxmox-ca.pem" in DEFAULT_SYNC_PATHS
    assert "secrets.enc.yaml" not in DEFAULT_SYNC_PATHS
    assert "ssh" not in DEFAULT_SYNC_PATHS


def test_default_sync_paths_include_only_the_public_proxmox_ca_from_state(tmp_path: Path):
    root = tmp_path / "homelab"
    trust = root / ".homelab-state" / "trust"
    trust.mkdir(parents=True)
    (trust / "proxmox-ca.pem").write_text("public trust anchor\n")
    (trust / "proxmox-ca-bundle.pem").write_text("host-specific system bundle\n")
    (root / ".homelab-state" / "controller.db").write_text("private state\n")

    archive = _build_tarball(root)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert ".homelab-state/trust/proxmox-ca.pem" in names
        assert ".homelab-state/trust/proxmox-ca-bundle.pem" not in names
        assert ".homelab-state/controller.db" not in names
    finally:
        archive.unlink(missing_ok=True)
        archive.parent.rmdir()


def test_role_tarball_contains_only_selected_runtime_env(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / "docker-compose.yml").parent.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text("services:\n  all-services: {}\n")
    for role in ("infra", "apps", "media"):
        env = root / "generated" / "bundles" / role / ".hooks.env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text(f"HOOK={role}\n")
        broad = root / "generated" / role / ".env"
        broad.parent.mkdir(parents=True, exist_ok=True)
        broad.write_text(f"RUNTIME={role}\n")
        (broad.parent / "compose.yaml").write_text(f"services:\n  {role}-only: {{}}\n")

    archive = _build_tarball(
        root,
        ("docker-compose.yml", "generated"),
        node="apps",
        control_node="infra",
    )
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            runtime_content = tar.extractfile("generated/apps/.env").read().decode()
            hook_content = tar.extractfile("generated/apps/.hooks.env").read().decode()
        assert names.count("generated/apps/.env") == 1
        assert names.count("generated/apps/.hooks.env") == 1
        assert not any(name.startswith("generated/bundles/") for name in names)
        assert "generated/infra/.env" not in names
        assert "generated/media/.env" not in names
        assert "docker-compose.yml" not in names
        assert "generated/apps/compose.yaml" in names
        assert "generated/infra/compose.yaml" not in names
        assert "generated/media/compose.yaml" not in names
        assert runtime_content == "RUNTIME=apps\n"
        assert hook_content == "HOOK=apps\n"
    finally:
        archive.unlink(missing_ok=True)
        archive.parent.rmdir()


def test_control_tarball_retains_all_enabled_node_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    for role in ("infra", "apps"):
        runtime = root / "generated" / role / ".env"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(f"RUNTIME={role}\n")
        (runtime.parent / "compose.yaml").write_text(f"services:\n  {role}: {{}}\n")
        bundle = root / "generated" / "bundles" / role / ".hooks.env"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text(f"HOOK={role}\n")

    archive = _build_tarball(
        root,
        ("generated",),
        node="infra",
        machine_ids=("infra", "apps"),
        control_node="infra",
    )
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        for role in ("infra", "apps"):
            assert f"generated/{role}/compose.yaml" in names
            assert f"generated/{role}/.env" in names
            assert f"generated/bundles/{role}/.hooks.env" in names
        assert "generated/infra/.hooks.env" in names
        assert "generated/apps/.hooks.env" not in names
    finally:
        archive.unlink(missing_ok=True)
        archive.parent.rmdir()


def test_tarball_filters_service_artifacts_by_node_owner(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    for role in ("infra", "media", "apps"):
        runtime = root / "generated" / role / ".env"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(f"RUNTIME={role}\n")
        bundle = root / "generated" / "bundles" / role / ".hooks.env"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(f"HOOK={role}\n")
    control_secret = root / "generated" / "authelia" / "configuration.yml"
    control_secret.parent.mkdir(parents=True)
    control_secret.write_text("control-secret\n")
    media_secret = root / "generated" / "recyclarr" / "recyclarr.yml"
    media_secret.parent.mkdir(parents=True)
    media_secret.write_text("media-secret\n")
    stale = root / "generated" / "undeclared-secret.yml"
    stale.write_text("must-not-sync\n")
    misplaced = root / "generated" / "media" / "nonlocal.conf"
    misplaced.write_text("must-not-sync\n")
    owners = {
        "generated/authelia/configuration.yml": "infra",
        "generated/recyclarr/recyclarr.yml": "media",
        "generated/media/nonlocal.conf": "infra",
    }

    workload_archive = _build_tarball(
        root,
        ("generated",),
        node="media",
        machine_ids=("infra", "media", "apps"),
        control_node="infra",
        generated_artifact_nodes=owners,
    )
    control_archive = _build_tarball(
        root,
        ("generated",),
        node="infra",
        machine_ids=("infra", "media", "apps"),
        control_node="infra",
        generated_artifact_nodes=owners,
    )
    try:
        with tarfile.open(workload_archive, "r:gz") as tar:
            workload_names = set(tar.getnames())
        with tarfile.open(control_archive, "r:gz") as tar:
            control_names = set(tar.getnames())

        assert "generated/recyclarr/recyclarr.yml" in workload_names
        assert "generated/authelia/configuration.yml" not in workload_names
        assert "generated/media/nonlocal.conf" not in workload_names
        assert "generated/undeclared-secret.yml" not in workload_names
        assert "generated/recyclarr/recyclarr.yml" in control_names
        assert "generated/authelia/configuration.yml" in control_names
        assert "generated/undeclared-secret.yml" not in control_names
    finally:
        for archive in (workload_archive, control_archive):
            archive.unlink(missing_ok=True)
            archive.parent.rmdir()


def test_workload_extract_cleans_other_role_runtime_and_hooks(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    _link_service_catalog(root)
    runtime = root / "generated" / "apps" / ".env"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("RUNTIME=apps\n")
    bundle = root / "generated" / "bundles" / "apps" / ".hooks.env"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("HOOK=apps\n")
    cfg = Config(domain="example.com", machines=machines_with_addresses(infra="10.0.0.1", apps="10.0.0.2"))

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.2", paths=("generated",))

    command = mock_ssh.call_args_list[0].args[2]
    assert "generated/infra/.env" in command
    assert "generated/infra/.hooks.env" in command


def test_workload_extract_preserves_authorized_artifact_roots_for_live_bind_mounts(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.manifest.artifacts import CompiledGeneratedArtifact

    root = tmp_path / "homelab"
    _link_service_catalog(root)
    runtime = root / "generated" / "apps" / ".env"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("RUNTIME=apps\n")
    bundle = root / "generated" / "bundles" / "apps" / ".hooks.env"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("HOOK=apps\n")
    recyclarr = root / "generated" / "recyclarr" / "recyclarr.yml"
    recyclarr.parent.mkdir(parents=True)
    recyclarr.write_text("radarr: {}\n")
    cfg = Config(domain="example.com", machines=machines_with_addresses(infra="10.0.0.1", apps="10.0.0.2"))
    monkeypatch.setattr(
        "toolkit.core.manifest.artifacts.compile_generated_artifacts",
        lambda *_args: (
            CompiledGeneratedArtifact(
                path="recyclarr/recyclarr.yml",
                service="recyclarr",
                node="apps",
                enabled=True,
                kind="file",
                mode="0600",
                sensitive=True,
            ),
        ),
    )

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.2", paths=("generated",))

    command = mock_ssh.call_args_list[0].args[2]
    assert "! -name apps" in command
    assert "! -name recyclarr" in command
    assert "--no-overwrite-dir" in command
    assert " --overwrite " not in command


def test_control_extract_cleans_disabled_node_runtime_and_recovery_bundle(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    _link_service_catalog(root)
    for role in ("infra", "apps"):
        runtime = root / "generated" / role / ".env"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(f"RUNTIME={role}\n")
        (runtime.parent / "compose.yaml").write_text(f"services:\n  {role}: {{}}\n")
        bundle = root / "generated" / "bundles" / role / ".hooks.env"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text(f"HOOK={role}\n")
    machines = machines_with_addresses(infra="10.0.0.1", apps="10.0.0.2")
    machines = {name: spec.model_copy(update={"enabled": name == "infra"}) for name, spec in machines.items()}
    cfg = Config(domain="example.com", machines=machines)

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.1", paths=("generated",))

    command = mock_ssh.call_args_list[0].args[2]
    assert "generated/apps/compose.yaml" in command
    assert "generated/apps/.env" in command
    assert "generated/apps/.hooks.env" in command
    assert "generated/bundles/apps/.hooks.env" in command


def test_non_infra_tarball_excludes_kopia_server_identity(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    server = root / "config" / "kopia"
    server.mkdir(parents=True)
    (server / "server.key").write_text("private")
    (server / "server.crt").write_text("public")
    runtime = root / "generated" / "apps" / ".env"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("RUNTIME=apps\n")
    bundle = root / "generated" / "bundles" / "apps" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("ROLE=apps\n")

    archive = _build_tarball(
        root,
        ("config", "generated"),
        node="apps",
        control_node="infra",
        config_source_nodes={"config/kopia": ("infra", True)},
    )
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert not any(name.startswith("config/kopia") for name in names)
    finally:
        archive.unlink(missing_ok=True)
        archive.parent.rmdir()


def test_config_tarball_scopes_declared_sources_and_keeps_static_files(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    for directory in ("kopia", "media-cache", "disabled"):
        (root / "config" / directory).mkdir(parents=True)
    (root / "config" / "static.yaml").write_text("static\n")
    (root / "config" / "kopia" / "server.key").write_text("kopia\n")
    (root / "config" / "media-cache" / "state.json").write_text("media\n")
    (root / "config" / "disabled" / "secret").write_text("disabled\n")
    for node in ("infra", "media", "apps"):
        env = root / "generated" / node / ".env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("NODE=1\n")
        (root / "generated" / "bundles" / node).mkdir(parents=True, exist_ok=True)
        (root / "generated" / "bundles" / node / ".hooks.env").write_text("HOOK=1\n")

    sources = {
        "config/kopia": ("infra", True),
        "config/media-cache": ("media", True),
        "config/disabled": ("apps", False),
    }

    def archive_names(node: str) -> set[str]:
        archive = _build_tarball(
            root,
            ("config",),
            node=node,
            control_node="infra",
            config_source_nodes=sources,
        )
        try:
            with tarfile.open(archive, "r:gz") as tar:
                return set(tar.getnames())
        finally:
            archive.unlink(missing_ok=True)
            archive.parent.rmdir()

    control_names = archive_names("infra")
    media_names = archive_names("media")
    apps_names = archive_names("apps")
    assert "config/static.yaml" in control_names
    assert "config/kopia/server.key" in control_names
    assert "config/media-cache/state.json" in control_names
    assert "config/disabled/secret" in control_names
    assert "config/static.yaml" in media_names
    assert "config/media-cache/state.json" in media_names
    assert "config/kopia/server.key" not in media_names
    assert "config/static.yaml" in apps_names
    assert not any(name.startswith("config/media-cache/") for name in apps_names)
    assert not any(name.startswith("config/disabled/") for name in apps_names)


def test_guest_sync_removes_previously_leaked_nonlocal_config(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    _link_service_catalog(root)
    (root / "config.yaml").write_text("domain: example.com\n")
    for node in ("infra", "apps"):
        runtime = root / "generated" / node / ".env"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text("NODE=1\n")
        bundle = root / "generated" / "bundles" / node / ".hooks.env"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("HOOK=1\n")
    cfg = Config(domain="example.com", machines=machines_with_addresses(infra="10.0.0.1", apps="10.0.0.2"))
    from toolkit.core.state.files import atomic_write_json

    atomic_write_json(
        root / ".homelab-state" / "service-ownership.json",
        {
            "version": 1,
            "generated": [],
            "config": [{"path": "config/kopia", "service": "kopia", "node": "infra"}],
            "machines": list(cfg.machines),
            "secrets": [],
        },
    )

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.2", paths=("config", "generated"))

    command = mock_ssh.call_args_list[0].args[2]
    assert "/opt/homelab/config/kopia" in command
    assert "rm -rf --" in command


def test_guest_sync_removes_disabled_artifact_inside_current_node(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.manifest.artifacts import CompiledGeneratedArtifact

    root = tmp_path / "homelab"
    _link_service_catalog(root)
    (root / "config.yaml").write_text("domain: example.com\n")
    for node in ("infra", "apps"):
        runtime = root / "generated" / node / ".env"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text("NODE=1\n")
        bundle = root / "generated" / "bundles" / node / ".hooks.env"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("HOOK=1\n")
    cfg = Config(domain="example.com", machines=machines_with_addresses(infra="10.0.0.1", apps="10.0.0.2"))
    monkeypatch.setattr(
        "toolkit.core.manifest.artifacts.compile_generated_artifacts",
        lambda *_args: (
            CompiledGeneratedArtifact(
                path="apps/stale.conf",
                service="disabled-service",
                node="apps",
                enabled=False,
                kind="file",
                mode="0600",
                sensitive=True,
            ),
        ),
    )

    with (
        patch("toolkit.core.deploy.repo_sync.scp_to_vm"),
        patch("toolkit.core.deploy.repo_sync.ssh_run_on_vm", return_value=(0, "ok", "")) as mock_ssh,
    ):
        sync_repo_to_guest(root, cfg, "10.0.0.2", paths=("generated",))

    command = mock_ssh.call_args_list[0].args[2]
    assert "/opt/homelab/generated/apps/stale.conf" in command
