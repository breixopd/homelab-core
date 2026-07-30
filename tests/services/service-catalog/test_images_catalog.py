"""Tests for custom image catalog and env generation."""

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli.images_cmd import images
from toolkit.core.config.config import Config
from toolkit.core.images.catalog import (
    compose_image_env,
    custom_images,
    expected_images_for_node,
    image_ref,
    images_for_node,
    resolve_image_names,
    resolve_image_tag,
)
from toolkit.core.images.locks import ImageLock, ResolvedImage


def test_image_ref():
    assert image_ref("ghcr.io/breixopd", "caddy", "latest") == "ghcr.io/breixopd/caddy:latest"
    assert image_ref("10.10.10.10:5000", "caddy") == "10.10.10.10:5000/caddy:latest"


def test_automatic_image_tag_uses_clean_checkout_commit(tmp_path: Path, monkeypatch) -> None:
    commit = "a" * 40
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    with patch("toolkit.core.images.catalog._git_output", side_effect=[f"{commit}\n", ""]):
        assert resolve_image_tag(tmp_path, "auto") == f"sha-{commit}"


def test_automatic_image_tag_fingerprints_modified_checkout(tmp_path: Path) -> None:
    commit = "b" * 40

    def git_output(_root: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return f"{commit}\n"
        if args[0] == "status":
            return " M toolkit/example.py\n"
        return "diff --git a/toolkit/example.py b/toolkit/example.py\n"

    with patch("toolkit.core.images.catalog._git_output", side_effect=git_output):
        tag = resolve_image_tag(tmp_path, "auto")

    assert tag.startswith("local-")
    assert len(tag) == len("local-") + 16


def test_explicit_image_tag_never_reads_git(tmp_path: Path) -> None:
    with patch("toolkit.core.images.catalog._git_output") as git_output:
        assert resolve_image_tag(tmp_path, "release-42") == "release-42"
    git_output.assert_not_called()


def test_images_for_vm_infra():
    names = {img.name for img in images_for_node(Config(), "infra")}
    assert "caddy" in names
    assert "headscale" in names
    assert "lldap" in names
    assert "homelab-ui" in names
    assert "music-sync" not in names


def test_compose_image_env_keys():
    env = compose_image_env("registry.example/homelab", "v1")
    assert env["HOMELAB_REGISTRY"] == "registry.example/homelab"
    assert env["HOMELAB_CADDY_IMAGE"] == "registry.example/homelab/caddy:v1"
    assert env["HOMELAB_UI_IMAGE"] == "registry.example/homelab/homelab-toolkit:v1"


def test_custom_images_are_discovered_from_service_manifests():
    images = custom_images()

    assert {image.name for image in images} == {
        "caddy",
        "headscale",
        "homelab-ui",
        "lldap",
        "loki",
        "prometheus",
    }
    assert next(image for image in images if image.name == "caddy").context == "toolkit/services/caddy/image"
    ui_image = next(image for image in images if image.name == "homelab-ui")
    assert ui_image.context == "."
    assert ui_image.repository == "homelab-toolkit"
    assert ui_image.platforms == ("linux/amd64", "linux/arm64")


def test_external_release_images_are_not_local_build_targets():
    names = {img.name for img in expected_images_for_node(Config(), "media")}
    assert "music-sync" not in names
    assert "media-cache" not in names


def test_resolve_image_names_unknown():
    try:
        resolve_image_names(("not-real",))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not-real" in str(exc)


def test_images_cli_emits_manifest_compiled_ci_matrix():
    root = Path(__file__).resolve().parents[3]

    result = CliRunner().invoke(images, ["list", "--ci", "--json"], obj={"root": str(root)})

    assert result.exit_code == 0
    plan = json.loads(result.output)
    assert {entry["name"] for entry in plan} == {
        "caddy",
        "headscale",
        "homelab-ui",
        "lldap",
        "loki",
        "prometheus",
    }
    assert next(entry for entry in plan if entry["name"] == "homelab-ui")["repository"] == "homelab-toolkit"
    assert next(entry for entry in plan if entry["name"] == "homelab-ui")["dockerfile"] == "toolkit/Dockerfile"
    assert all(entry["platforms"] == "linux/amd64,linux/arm64" for entry in plan)
    assert all(entry["context"].startswith("toolkit/services/") for entry in plan if entry["name"] != "homelab-ui")


def test_images_sync_cli_passes_explicit_source_policy(tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    with (
        patch("toolkit.cli.images_cmd.load_root_config", return_value=(tmp_path, cfg)),
        patch("toolkit.cli.images_cmd.sync_images_to_guests", return_value=[]) as sync,
    ):
        result = CliRunner().invoke(
            images,
            ["sync", "--node", "infra", "--image", "caddy", "--source", "registry"],
            obj={"root": str(tmp_path)},
        )

    assert result.exit_code == 0
    assert sync.call_args.kwargs["source"] == "registry"
    assert sync.call_args.kwargs["names"] == ("caddy",)


def test_images_audit_cli_defaults_to_all_declared_audits(tmp_path: Path) -> None:
    with patch("toolkit.cli.images_cmd.audit_images", return_value=["one", "two"]) as audit:
        result = CliRunner().invoke(images, ["audit"], obj={"root": str(tmp_path)})

    assert result.exit_code == 0
    assert result.output.endswith("Audited 2 image(s)\n")
    assert audit.call_args.kwargs["names"] is None


def test_images_verify_cli_passes_requested_images(tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    with (
        patch("toolkit.cli.images_cmd.load_root_config", return_value=(tmp_path, cfg)),
        patch("toolkit.cli.images_cmd.verify_guest_images", return_value=(True, [])) as verify,
    ):
        result = CliRunner().invoke(
            images,
            ["verify", "--node", "infra", "--image", "caddy"],
            obj={"root": str(tmp_path)},
        )

    assert result.exit_code == 0
    assert verify.call_args.kwargs["names"] == ("caddy",)


def test_images_sync_cli_rejects_removed_no_build_flag(tmp_path: Path) -> None:
    with patch("toolkit.cli.images_cmd.load_root_config", return_value=(tmp_path, Config(domain="example.test"))):
        result = CliRunner().invoke(images, ["sync", "--no-build"], obj={"root": str(tmp_path)})

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert "--no-build" in result.output


def test_images_lock_cli_reports_progress_and_writes_compose(tmp_path: Path) -> None:
    compose = tmp_path / "toolkit/services/example/compose.yaml"
    lock = ImageLock(
        plugin="example",
        runtime="example",
        compose_path=compose,
        current_ref="ghcr.io/example/service:v1.2.3",
        version_ref="ghcr.io/example/service:v1.2.3",
        digest=None,
    )
    resolved = ResolvedImage(lock.version_ref, "sha256:" + ("a" * 64), ("linux/amd64", "linux/arm64"))

    def resolve(_locks, *, max_workers, on_progress):
        assert max_workers == 4
        on_progress(1, 1, resolved)
        return {resolved.version_ref: resolved}

    with (
        patch("toolkit.cli.images_cmd.discover_image_locks", return_value=(lock,)),
        patch("toolkit.cli.images_cmd.resolve_image_locks", side_effect=resolve),
        patch("toolkit.cli.images_cmd.apply_image_locks", return_value=(compose,)) as apply,
    ):
        result = CliRunner().invoke(
            images,
            ["lock", "--write", "--workers", "4"],
            obj={"root": str(tmp_path)},
        )

    assert result.exit_code == 0
    assert "[1/1] ghcr.io/example/service:v1.2.3" in result.output
    assert "linux/amd64, linux/arm64" in result.output
    assert "Updated 1 service Compose file(s)" in result.output
    assert apply.call_args.args[1] == {resolved.version_ref: resolved.digest}


def test_images_lock_refresh_reports_moved_tag_without_writing(tmp_path: Path) -> None:
    compose = tmp_path / "toolkit/services/example/compose.yaml"
    lock = ImageLock(
        plugin="example",
        runtime="example",
        compose_path=compose,
        current_ref="ghcr.io/example/service:v1@sha256:" + ("a" * 64),
        version_ref="ghcr.io/example/service:v1",
        digest="sha256:" + ("a" * 64),
    )
    resolved = ResolvedImage(lock.version_ref, "sha256:" + ("b" * 64), ("linux/amd64",))

    with (
        patch("toolkit.cli.images_cmd.discover_image_locks", return_value=(lock,)),
        patch("toolkit.cli.images_cmd.load_image_lock_cache", return_value={lock.version_ref: resolved}),
        patch("toolkit.cli.images_cmd.apply_image_locks") as apply,
    ):
        result = CliRunner().invoke(
            images,
            ["lock", "--refresh"],
            obj={"root": str(tmp_path)},
        )

    assert result.exit_code == 0
    assert "Detected digest drift in 1 runtime declaration(s)" in result.output
    assert "no files changed without --write" in result.output
    apply.assert_not_called()
