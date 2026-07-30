from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from toolkit.core.config.config import Config, ImagesConfig
from toolkit.core.images.publish import (
    audit_images,
    build_images,
    smoke_test_images,
    sync_images_to_guests,
    verify_guest_images,
)


def test_build_images_targets_requested_platform(tmp_path: Path) -> None:
    image = SimpleNamespace(
        name="example",
        repository="example",
        context="image",
        dockerfile=None,
        platforms=("linux/amd64", "linux/arm64"),
    )
    (tmp_path / "image").mkdir()
    completed = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[image]),
        patch("toolkit.core.images.publish.subprocess.run", return_value=completed) as run,
    ):
        build_images(tmp_path, names=("example",), platform="linux/arm64")

    assert run.call_args.args[0] == [
        "docker",
        "build",
        "--platform",
        "linux/arm64",
        "--build-arg",
        "TARGETOS=linux",
        "--build-arg",
        "TARGETARCH=arm64",
        "-t",
        "ghcr.io/breixopd/example:latest",
        str(tmp_path / "image"),
    ]


def test_guest_image_sync_uses_root_private_unique_archive(tmp_path):
    cfg = Config(domain="example.com")
    saved = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    with (
        patch(
            "toolkit.core.images.publish.expected_images_for_node",
            return_value=[
                SimpleNamespace(
                    name="caddy",
                    repository="caddy",
                    platforms=("linux/amd64", "linux/arm64"),
                )
            ],
        ),
        patch("toolkit.core.images.publish.subprocess.run", return_value=saved),
        patch("toolkit.core.images.publish.build_images"),
        patch("toolkit.core.images.publish.scp_to_vm") as scp,
        patch(
            "toolkit.core.images.publish.ssh_run_on_vm",
            side_effect=[(0, "linux/amd64\n", ""), (0, "loaded", "")],
        ) as ssh,
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="local")

    remote_archive = scp.call_args.args[4]
    assert re.fullmatch(r"/root/\.homelab-images-infra-[0-9a-f]{24}\.tar\.gz", remote_archive)
    assert scp.call_args.args[2].suffixes[-2:] == [".tar", ".gz"]
    assert scp.call_args.kwargs["timeout"] == 1800
    command = ssh.call_args.args[2]
    assert remote_archive in command
    assert "trap" in command
    assert 'docker load -i "$archive"' in command
    assert "docker tag sha256:" + "a" * 64 in command
    assert "docker image inspect --format '{{.Id}}'" in command
    assert 'test "$actual" = sha256:' + "a" * 64 in command


def test_guest_image_sync_reports_transfer_timeout_without_traceback(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(
        name="caddy",
        repository="caddy",
        platforms=("linux/amd64", "linux/arm64"),
    )
    completed = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch(
            "toolkit.core.images.publish.ssh_run_on_vm",
            return_value=(0, "linux/amd64\n", ""),
        ),
        patch("toolkit.core.images.publish.build_images"),
        patch("toolkit.core.images.publish.subprocess.run", return_value=completed),
        patch(
            "toolkit.core.images.publish.scp_to_vm",
            side_effect=RuntimeError("scp to 10.10.10.10 timed out after 1800s"),
        ),
        pytest.raises(
            RuntimeError,
            match=r"image transfer to infra failed for .*caddy.*timed out after 1800s",
        ),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="local")


def test_guest_image_sync_reports_local_docker_save_failure(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(
        name="caddy",
        repository="caddy",
        platforms=("linux/amd64", "linux/arm64"),
    )
    failed_save = MagicMock(returncode=1, stdout="", stderr="disk full")

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch(
            "toolkit.core.images.publish.ssh_run_on_vm",
            return_value=(0, "linux/amd64\n", ""),
        ),
        patch("toolkit.core.images.publish.build_images"),
        patch(
            "toolkit.core.images.publish._local_image_identity",
            return_value="sha256:" + "a" * 64,
        ),
        patch("toolkit.core.images.publish.subprocess.run", return_value=failed_save),
        patch("toolkit.core.images.publish.scp_to_vm") as scp,
        pytest.raises(RuntimeError, match=r"docker save failed for infra: disk full"),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="local")

    scp.assert_not_called()


def test_guest_image_sync_reports_remote_docker_load_failure(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(
        name="caddy",
        repository="caddy",
        platforms=("linux/amd64", "linux/arm64"),
    )
    saved = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch(
            "toolkit.core.images.publish.ssh_run_on_vm",
            side_effect=[(0, "linux/amd64\n", ""), (1, "", "invalid archive")],
        ),
        patch("toolkit.core.images.publish.build_images"),
        patch("toolkit.core.images.publish.subprocess.run", return_value=saved),
        patch("toolkit.core.images.publish.scp_to_vm"),
        pytest.raises(
            RuntimeError,
            match=r"docker load on infra failed for .*caddy.*: invalid archive",
        ),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="local")


def test_guest_image_sync_pulls_from_registry_without_local_docker(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", images=ImagesConfig(tag="release-test"))
    image = SimpleNamespace(name="caddy", repository="caddy")

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.images.publish.ssh_run_on_vm", return_value=(0, "pulled", "")) as ssh,
        patch("toolkit.core.images.publish.build_images") as build,
        patch("toolkit.core.images.publish.scp_to_vm") as scp,
    ):
        lines = sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="auto")

    assert "docker pull ghcr.io/breixopd/caddy:release-test" in ssh.call_args.args[2]
    assert any("pulled ghcr.io/breixopd/caddy:release-test" in line for line in lines)
    build.assert_not_called()
    scp.assert_not_called()


def test_guest_image_sync_filters_to_requested_images(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", images=ImagesConfig(tag="release-test"))
    images = [
        SimpleNamespace(name="caddy", repository="caddy"),
        SimpleNamespace(name="headscale", repository="headscale"),
    ]

    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[images[0]]) as resolve,
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=images),
        patch("toolkit.core.images.publish.ssh_run_on_vm", return_value=(0, "pulled", "")) as ssh,
    ):
        sync_images_to_guests(
            tmp_path,
            cfg,
            vms=("infra",),
            names=("caddy",),
            source="registry",
        )

    resolve.assert_called_once_with(("caddy",), tmp_path.resolve())
    pull_commands = [call.args[2] for call in ssh.call_args_list]
    assert pull_commands == ["docker pull ghcr.io/breixopd/caddy:release-test"]


def test_guest_image_sync_rejects_image_not_enabled_on_target(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(name="caddy", repository="caddy")

    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[image]),
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[]),
        pytest.raises(ValueError, match="caddy"),
    ):
        sync_images_to_guests(
            tmp_path,
            cfg,
            vms=("media",),
            names=("caddy",),
            source="local",
        )


def test_guest_image_verify_filters_to_requested_images(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", images=ImagesConfig(tag="release-test"))
    images = [
        SimpleNamespace(name="caddy", repository="caddy"),
        SimpleNamespace(name="headscale", repository="headscale"),
    ]

    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[images[0]]),
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=images),
        patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "", "")) as ssh,
    ):
        ok, lines = verify_guest_images(
            cfg,
            tmp_path,
            vms=("infra",),
            names=("caddy",),
        )

    assert ok is True
    assert lines == ["infra: all 1 custom image(s) present"]
    verify_command = ssh.call_args.args[2]
    assert "ghcr.io/breixopd/caddy:release-test" in verify_command
    assert "docker image inspect --format '{{.Id}}'" in verify_command
    assert "sha256:" in verify_command


def test_guest_image_sync_uses_registry_and_tag_from_config(tmp_path: Path) -> None:
    cfg = Config(
        domain="example.com",
        images=ImagesConfig(registry="registry.example/acme", tag="release-42", source="registry"),
    )
    image = SimpleNamespace(name="caddy", repository="caddy")
    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.images.publish.ssh_run_on_vm", return_value=(0, "pulled", "")) as ssh,
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",))

    assert "docker pull registry.example/acme/caddy:release-42" in ssh.call_args.args[2]


def test_guest_image_sync_uses_ephemeral_registry_credentials(tmp_path: Path) -> None:
    cfg = Config(
        domain="example.com",
        images=ImagesConfig(
            registry="ghcr.io/private-owner",
            source="registry",
            auth={"username": "automation", "token_secret": "GHCR_READ_TOKEN"},
        ),
    )
    image = SimpleNamespace(name="caddy", repository="caddy")
    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={"GHCR_READ_TOKEN": "token-value"}),
        patch("toolkit.core.images.publish.ssh_run_on_vm", return_value=(0, "pulled", "")) as ssh,
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",))

    command = ssh.call_args.args[2]
    assert "docker --config" in command
    assert "login ghcr.io" in command
    assert "--password-stdin" in command
    assert "token-value" not in command
    assert ssh.call_args.kwargs["stdin"] == "token-value\n"


def test_guest_image_sync_fails_before_ssh_when_registry_secret_is_missing(tmp_path: Path) -> None:
    cfg = Config(
        domain="example.com",
        images=ImagesConfig(
            registry="ghcr.io/private-owner",
            source="auto",
            auth={"username": "automation", "token_secret": "GHCR_READ_TOKEN"},
        ),
    )
    image = SimpleNamespace(name="caddy", repository="caddy")
    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch("toolkit.core.images.publish.ssh_run_on_vm") as ssh,
        pytest.raises(RuntimeError, match="registry auth secret GHCR_READ_TOKEN is missing"),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",))

    ssh.assert_not_called()


def test_auto_image_sync_builds_and_transfers_only_registry_misses(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", images=ImagesConfig(tag="release-test"))
    images = [
        SimpleNamespace(
            name="caddy",
            repository="caddy",
            platforms=("linux/amd64", "linux/arm64"),
        ),
        SimpleNamespace(
            name="headscale",
            repository="headscale",
            platforms=("linux/amd64", "linux/arm64"),
        ),
    ]
    saved = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    def remote(_cfg, _ip, command, **_kwargs):
        if "docker pull" in command and "headscale" in command:
            return 1, "", "network timeout"
        if "docker info" in command:
            return 0, "linux/aarch64\n", ""
        return 0, "loaded", ""

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=images),
        patch("toolkit.core.images.publish.ssh_run_on_vm", side_effect=remote) as ssh,
        patch(
            "toolkit.core.images.publish.build_images",
            return_value=["ghcr.io/breixopd/headscale:release-test"],
        ) as build,
        patch("toolkit.core.images.publish.subprocess.run", return_value=saved) as run,
        patch("toolkit.core.images.publish.scp_to_vm"),
        patch("toolkit.core.images.publish.time.sleep"),
    ):
        lines = sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="auto")

    build.assert_called_once_with(
        tmp_path.resolve(),
        registry="ghcr.io/breixopd",
        tag="release-test",
        names=("headscale",),
        platform="linux/arm64",
        docker_bin="docker",
        on_log=ANY,
    )
    assert run.call_args_list[0].args[0][:3] == ["docker", "image", "inspect"]
    assert run.call_args_list[1].args[0][:3] == ["docker", "save", "-o"]
    assert run.call_args_list[1].args[0][-1] == "sha256:" + "a" * 64
    assert run.call_args_list[2].args[0][:3] == ["gzip", "-1", "-f"]
    assert sum("docker pull" in call.args[2] and "headscale" in call.args[2] for call in ssh.call_args_list) == 3
    assert any("local fallback" in line for line in lines)


def test_local_image_sync_groups_builds_by_guest_platform(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(
        name="caddy",
        repository="caddy",
        platforms=("linux/amd64", "linux/arm64"),
    )
    saved = MagicMock(returncode=0, stdout="sha256:" + "a" * 64, stderr="")

    def remote(_cfg, ip, command, **_kwargs):
        if "docker info" in command:
            architecture = "x86_64" if str(ip).endswith(".10") else "aarch64"
            return 0, f"linux/{architecture}\n", ""
        return 0, "loaded", ""

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.images.publish.ssh_run_on_vm", side_effect=remote),
        patch("toolkit.core.images.publish.build_images") as build,
        patch("toolkit.core.images.publish.subprocess.run", return_value=saved),
        patch("toolkit.core.images.publish.scp_to_vm"),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra", "media"), source="local")

    assert [call.kwargs["platform"] for call in build.call_args_list] == ["linux/amd64", "linux/arm64"]


def test_local_image_sync_rejects_unsupported_guest_platform(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(name="caddy", repository="caddy", platforms=("linux/amd64",))

    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch(
            "toolkit.core.images.publish.ssh_run_on_vm",
            return_value=(0, "linux/aarch64\n", ""),
        ),
        patch("toolkit.core.images.publish.build_images") as build,
        pytest.raises(RuntimeError, match="caddy does not support linux/arm64"),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="local")

    build.assert_not_called()


def test_registry_image_source_fails_closed_without_building(tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    image = SimpleNamespace(name="caddy", repository="caddy")
    with (
        patch("toolkit.core.images.publish.expected_images_for_node", return_value=[image]),
        patch("toolkit.core.images.publish.ssh_run_on_vm", return_value=(1, "", "denied")) as ssh,
        patch("toolkit.core.images.publish.build_images") as build,
        patch("toolkit.core.images.publish.time.sleep"),
        pytest.raises(RuntimeError, match="registry pull failed"),
    ):
        sync_images_to_guests(tmp_path, cfg, vms=("infra",), source="registry")

    build.assert_not_called()
    assert ssh.call_count == 1


def test_images_config_defaults_to_auto_and_rejects_unknown_source() -> None:
    assert ImagesConfig().source == "auto"
    assert ImagesConfig().tag == "auto"
    with pytest.raises(ValueError):
        ImagesConfig(source="sometimes")
    with pytest.raises(ValueError):
        ImagesConfig(registry="https://ghcr.io/example")
    with pytest.raises(ValueError):
        ImagesConfig(tag="not a tag")
    with pytest.raises(ValueError):
        ImagesConfig(auth={"username": "automation"})


def test_image_smoke_tests_use_declared_entrypoint_command_and_output(tmp_path: Path) -> None:
    image = SimpleNamespace(
        name="example",
        repository="example",
        smoke_tests=(SimpleNamespace(entrypoint="python", command=("-c", "print('ready')"), contains="ready"),),
    )
    completed = MagicMock(returncode=0, stdout="ready\n", stderr="")
    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[image]),
        patch("toolkit.core.images.publish.subprocess.run", return_value=completed) as run,
    ):
        assert smoke_test_images(tmp_path, registry="local", tag="test", names=("example",)) == ["example"]

    assert run.call_args.args[0] == [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "local/example:test",
        "-c",
        "print('ready')",
    ]


def test_image_smoke_test_requires_declared_output(tmp_path: Path) -> None:
    image = SimpleNamespace(
        name="example",
        repository="example",
        smoke_tests=(SimpleNamespace(entrypoint="", command=("--version",), contains="expected"),),
    )
    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[image]),
        patch(
            "toolkit.core.images.publish.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="different", stderr=""),
        ),
        pytest.raises(RuntimeError, match="expected text"),
    ):
        smoke_test_images(tmp_path, names=("example",))


def test_image_dependency_audit_uses_manifest_requirements(tmp_path: Path) -> None:
    image = SimpleNamespace(
        name="example",
        requirements="toolkit/services/example/image/requirements.txt",
    )
    with (
        patch("toolkit.core.images.publish.resolve_image_names", return_value=[image]),
        patch(
            "toolkit.core.images.publish.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        assert audit_images(tmp_path, names=("example",)) == ["example"]

    assert run.call_args.args[0] == [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(tmp_path / image.requirements),
    ]
