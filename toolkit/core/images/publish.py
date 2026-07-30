"""Build, push, and sync custom Docker images without GitHub Actions."""

from __future__ import annotations

import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from toolkit.core.ansible.ansible_ssh import scp_to_vm, ssh_run_on_vm
from toolkit.core.config.config import Config
from toolkit.core.images.catalog import (
    DEFAULT_REGISTRY,
    DEFAULT_TAG,
    CustomImage,
    expected_images_for_node,
    image_ref,
    resolve_image_names,
    resolve_image_tag,
)

LogFn = Callable[[str], None]
ImageSource = Literal["auto", "registry", "local"]
ImagePlatform = Literal["linux/amd64", "linux/arm64"]
_PULL_ATTEMPTS = 3
_RETRYABLE_PULL_ERRORS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "connection",
    "eof",
    "i/o timeout",
    "network",
    "rate limit",
    "temporary",
    "timeout",
    "tls handshake",
    "too many requests",
)


def _log_default(msg: str) -> None:
    pass


def build_images(
    root: Path,
    *,
    registry: str = DEFAULT_REGISTRY,
    tag: str = DEFAULT_TAG,
    names: tuple[str, ...] | None = None,
    platform: ImagePlatform | None = None,
    docker_bin: str = "docker",
    on_log: LogFn | None = None,
) -> list[str]:
    """Build custom images locally with registry tags (no push required)."""
    log = on_log or _log_default
    root = root.resolve()
    built: list[str] = []
    for img in resolve_image_names(names, root):
        ref = image_ref(registry, img.repository, tag)
        context = root / img.context
        if not context.is_dir():
            raise FileNotFoundError(f"Missing build context: {context}")
        if platform is not None and platform not in img.platforms:
            raise RuntimeError(f"{img.name} does not support {platform}")
        log(f"Building {ref} from {img.context}")
        build_cmd = [docker_bin, "build"]
        if platform is not None:
            target_os, target_arch = platform.split("/", 1)
            build_cmd.extend(
                [
                    "--platform",
                    platform,
                    "--build-arg",
                    f"TARGETOS={target_os}",
                    "--build-arg",
                    f"TARGETARCH={target_arch}",
                ]
            )
        build_cmd.extend(["-t", ref])
        if img.dockerfile:
            build_cmd.extend(["-f", str(root / img.dockerfile)])
        build_cmd.append(str(context))
        proc = subprocess.run(
            build_cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "build failed")[:400]
            raise RuntimeError(f"Build failed for {img.name}: {err}")
        built.append(ref)
        log(f"Built {ref}")
    return built


def push_images(
    *,
    root: Path | None = None,
    registry: str = DEFAULT_REGISTRY,
    tag: str = DEFAULT_TAG,
    names: tuple[str, ...] | None = None,
    docker_bin: str = "docker",
    on_log: LogFn | None = None,
) -> list[str]:
    """Push previously built images to a registry (run `docker login` first)."""
    log = on_log or _log_default
    pushed: list[str] = []
    for img in resolve_image_names(names, root):
        ref = image_ref(registry, img.repository, tag)
        log(f"Pushing {ref}")
        proc = subprocess.run(
            [docker_bin, "push", ref],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "push failed")[:400]
            raise RuntimeError(f"Push failed for {ref}: {err}")
        pushed.append(ref)
        log(f"Pushed {ref}")
    return pushed


def _pull_guest_image(
    cfg: Config,
    root: Path,
    *,
    vm: str,
    ref: str,
    registry: str,
    auth: tuple[str, str] | None,
    log: LogFn,
) -> tuple[bool, str]:
    """Pull one image on a guest with bounded retries."""
    vm_ip = cfg.node_ip(vm)
    detail = "pull failed"
    for attempt in range(1, _PULL_ATTEMPTS + 1):
        log(f"{vm}: pulling {ref} ({attempt}/{_PULL_ATTEMPTS})")
        stdin = None
        if auth is None:
            command = f"docker pull {shlex.quote(ref)}"
        else:
            username, token = auth
            registry_host = registry.split("/", 1)[0]
            command = (
                "set -eu; auth_dir=$(mktemp -d); trap 'rm -rf \"$auth_dir\"' EXIT; "
                f'docker --config "$auth_dir" login {shlex.quote(registry_host)} '
                f"--username {shlex.quote(username)} --password-stdin >/dev/null; "
                f'docker --config "$auth_dir" pull {shlex.quote(ref)}'
            )
            stdin = token + "\n"
        rc, out, err = ssh_run_on_vm(
            cfg,
            vm_ip,
            command,
            root=root,
            timeout=600,
            stdin=stdin,
        )
        if rc == 0:
            output_lines = (out or "").strip().splitlines()
            return True, output_lines[-1] if output_lines else "pulled"
        detail = (err or out or "pull failed").strip()[:240]
        retryable = any(marker in detail.lower() for marker in _RETRYABLE_PULL_ERRORS)
        if retryable and attempt < _PULL_ATTEMPTS:
            time.sleep(2 ** (attempt - 1))
        else:
            break
    return False, detail


def _load_guest_images(
    root: Path,
    cfg: Config,
    *,
    vm: str,
    refs: list[str],
    docker_bin: str,
) -> list[str]:
    """Transfer locally available refs in bounded root-private archives.

    Docker's archive loader can fail part-way through a large multi-image
    stream, leaving a guest with an ambiguous partial revision.  One archive
    per image keeps retries bounded and makes the failing image explicit while
    retaining the same private SSH transport.
    """
    lines: list[str] = []
    for ref in refs:
        lines.extend(_load_guest_image(root, cfg, vm=vm, ref=ref, docker_bin=docker_bin))
    return lines


def _load_guest_image(
    root: Path,
    cfg: Config,
    *,
    vm: str,
    ref: str,
    docker_bin: str,
) -> list[str]:
    """Transfer and load one locally available image on a guest."""
    vm_ip = cfg.node_ip(vm)
    with tempfile.TemporaryDirectory(prefix="homelab-img-") as tmpdir:
        image_label = ref.rsplit("/", 1)[-1].replace(":", "-").replace("@", "-")
        archive = Path(tmpdir) / f"{vm}-{image_label}.tar"
        identity = _local_image_identity(ref, docker_bin=docker_bin)
        proc = subprocess.run(
            [docker_bin, "save", "-o", str(archive), identity],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "docker save failed")[:300]
            raise RuntimeError(f"docker save failed for {vm}: {err}")
        # Docker archives are sparse but compress well.  Sending the raw tar can
        # exceed the SSH timeout on remote hosts (the toolkit image alone can be
        # close to 1 GiB), while ``docker load`` transparently accepts gzip.
        compressed_archive = Path(f"{archive}.gz")
        compress = subprocess.run(
            ["gzip", "-1", "-f", str(archive)],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if compress.returncode != 0:
            err = (compress.stderr or compress.stdout or "gzip failed")[:300]
            raise RuntimeError(f"image compression failed for {vm}: {err}")

        remote_archive = f"/root/.homelab-images-{vm}-{secrets.token_hex(12)}.tar.gz"
        try:
            scp_to_vm(cfg, root, compressed_archive, vm_ip, remote_archive, timeout=1800)
        except RuntimeError as exc:
            raise RuntimeError(f"image transfer to {vm} failed for {ref}: {exc}") from exc

        quoted_archive = shlex.quote(remote_archive)
        expected_id = shlex.quote(identity)
        load_cmd = (
            f"set -euo pipefail; archive={quoted_archive}; trap 'rm -f \"$archive\"' EXIT; "
            'docker load -i "$archive" >/dev/null; '
            f"docker image inspect {expected_id} >/dev/null; "
            f"docker tag {expected_id} {shlex.quote(ref)}; "
            f"actual=$(docker image inspect --format '{{{{.Id}}}}' {shlex.quote(ref)}); "
            f'test "$actual" = {expected_id}'
        )
        load_rc, load_out, load_err = ssh_run_on_vm(cfg, vm_ip, load_cmd, root=root, timeout=600)
        if load_rc != 0:
            err = (load_err or load_out or "docker load failed")[:300]
            raise RuntimeError(f"docker load on {vm} failed for {ref}: {err}")

    lines = [f"{vm}: {line.strip()}" for line in (load_out or "").splitlines() if line.strip()]
    lines.append(f"{vm}: loaded {ref} from local fallback")
    return lines


def _local_image_identity(ref: str, *, docker_bin: str = "docker") -> str:
    """Resolve the immutable local image ID before transferring an archive."""
    proc = subprocess.run(
        [docker_bin, "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    identity = (proc.stdout or "").strip()
    if proc.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
        raise RuntimeError(f"Unable to resolve immutable image ID for {ref}")
    return identity


def _guest_container_platform(cfg: Config, root: Path, *, vm: str) -> ImagePlatform:
    """Return the normalized Docker platform reported by one managed guest."""
    rc, out, err = ssh_run_on_vm(
        cfg,
        cfg.node_ip(vm),
        "docker info --format '{{.OSType}}/{{.Architecture}}'",
        root=root,
        timeout=30,
    )
    if rc != 0:
        detail = (err or out or "docker info failed").strip()[:240]
        raise RuntimeError(f"Unable to detect Docker platform on {vm}: {detail}")

    raw_platform = (out or "").strip().lower()
    os_name, separator, architecture = raw_platform.partition("/")
    normalized_architecture = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(architecture)
    if separator != "/" or os_name != "linux" or normalized_architecture is None:
        raise RuntimeError(f"Unsupported Docker platform on {vm}: {raw_platform or 'empty response'}")
    return cast(ImagePlatform, f"linux/{normalized_architecture}")


def _selected_images_by_vm(
    cfg: Config,
    root: Path,
    target_vms: list[str],
    names: tuple[str, ...] | None,
) -> dict[str, list[CustomImage]]:
    selected_names = {image.name for image in resolve_image_names(names, root)} if names else None
    images_by_vm: dict[str, list[CustomImage]] = {}
    for vm in target_vms:
        expected = expected_images_for_node(cfg, vm, root)
        if selected_names is not None:
            expected = [image for image in expected if image.name in selected_names]
        if expected:
            images_by_vm[vm] = expected
    if selected_names is not None:
        placed_names = {image.name for images in images_by_vm.values() for image in images}
        unavailable = sorted(selected_names - placed_names)
        if unavailable:
            raise ValueError(f"selected image(s) not enabled on target machines: {', '.join(unavailable)}")
    return images_by_vm


def sync_images_to_guests(
    root: Path,
    cfg: Config,
    *,
    registry: str | None = None,
    tag: str | None = None,
    vms: tuple[str, ...] | None = None,
    names: tuple[str, ...] | None = None,
    source: ImageSource | None = None,
    docker_bin: str = "docker",
    on_log: LogFn | None = None,
) -> list[str]:
    """Reconcile guest images from a registry, local builds, or automatic fallback."""
    log = on_log or _log_default
    root = root.resolve()
    target_vms = list(vms or cfg.enabled_nodes)
    selected_registry = registry or cfg.images.registry
    selected_tag = resolve_image_tag(root, tag or cfg.images.tag)
    selected_source = source or cfg.images.source
    if selected_source not in {"auto", "registry", "local"}:
        raise ValueError(f"unsupported image source: {selected_source}")
    images_by_vm = _selected_images_by_vm(cfg, root, target_vms, names)

    logs: list[str] = []
    fallback_by_vm: dict[str, list[CustomImage]] = {}
    failures: list[str] = []
    auth: tuple[str, str] | None = None
    if selected_source != "local":
        auth_contract = cfg.images.auth
        if auth_contract.token_secret:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.secrets.secrets import load_secrets_plaintext

            token = load_secrets_plaintext(secrets_path(root)).get(auth_contract.token_secret, "")
            if not token:
                raise RuntimeError(f"registry auth secret {auth_contract.token_secret} is missing")
            auth = (auth_contract.username, token)
        for vm, images in images_by_vm.items():
            for image in images:
                ref = image_ref(selected_registry, image.repository, selected_tag)
                pulled, detail = _pull_guest_image(
                    cfg,
                    root,
                    vm=vm,
                    ref=ref,
                    registry=selected_registry,
                    auth=auth,
                    log=log,
                )
                if pulled:
                    logs.append(f"{vm}: pulled {ref}")
                    continue
                failure = f"{vm}: registry pull failed for {ref}: {detail}"
                log(failure)
                logs.append(failure)
                failures.append(f"{vm}: {ref} ({detail})")
                fallback_by_vm.setdefault(vm, []).append(image)
        if failures and selected_source == "registry":
            raise RuntimeError("registry pull failed: " + "; ".join(failures))
    else:
        fallback_by_vm = {vm: list(images) for vm, images in images_by_vm.items()}

    if fallback_by_vm:
        fallback_by_platform: dict[ImagePlatform, dict[str, list[CustomImage]]] = {}
        for vm, images in fallback_by_vm.items():
            platform = _guest_container_platform(cfg, root, vm=vm)
            for image in images:
                if platform not in image.platforms:
                    raise RuntimeError(f"{image.name} does not support {platform} required by {vm}")
            fallback_by_platform.setdefault(platform, {})[vm] = images

        image_count = len({image.name for images in fallback_by_vm.values() for image in images})
        if selected_source == "auto":
            log(f"Registry unavailable for {image_count} image(s); using local fallback")
        else:
            log(f"Building {image_count} image(s) from local service sources")
        for platform, platform_fallbacks in fallback_by_platform.items():
            build_names = tuple(dict.fromkeys(image.name for images in platform_fallbacks.values() for image in images))
            log(f"Building {len(build_names)} image(s) for {platform}")
            build_images(
                root,
                registry=selected_registry,
                tag=selected_tag,
                names=build_names,
                platform=platform,
                docker_bin=docker_bin,
                on_log=log,
            )
            for vm, images in platform_fallbacks.items():
                refs = [image_ref(selected_registry, image.repository, selected_tag) for image in images]
                log(f"{vm}: transferring {len(refs)} locally built image(s)")
                logs.extend(_load_guest_images(root, cfg, vm=vm, refs=refs, docker_bin=docker_bin))

    return logs


def verify_guest_images(
    cfg: Config,
    root: Path,
    *,
    registry: str | None = None,
    tag: str | None = None,
    vms: tuple[str, ...] | None = None,
    names: tuple[str, ...] | None = None,
    on_log: LogFn | None = None,
) -> tuple[bool, list[str]]:
    """Check expected custom images exist on each guest (`docker image inspect`)."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    log = on_log or _log_default
    ok = True
    lines: list[str] = []
    target_vms = list(vms or cfg.enabled_nodes)
    selected_registry = registry or cfg.images.registry
    selected_tag = resolve_image_tag(root, tag or cfg.images.tag)

    for vm, expected in _selected_images_by_vm(cfg, root.resolve(), target_vms, names).items():
        missing: list[str] = []
        for img in expected:
            ref = image_ref(selected_registry, img.repository, selected_tag)
            cmd = (
                "set -euo pipefail; "
                f"identity=$(docker image inspect --format '{{{{.Id}}}}' {shlex.quote(ref)}); "
                "printf '%s\\n' \"$identity\" | grep -Eq '^sha256:[0-9a-f]{64}$'"
            )
            rc, _out, _err = ssh_run_on_vm(cfg, cfg.node_ip(vm), cmd, root=root, timeout=30)
            if rc != 0:
                missing.append(img.name)
        if missing:
            ok = False
            msg = f"{vm}: missing images: {', '.join(missing)} (run: homelab-toolkit images sync --node {vm})"
            lines.append(msg)
            log(msg)
        else:
            msg = f"{vm}: all {len(expected)} custom image(s) present"
            lines.append(msg)
            log(msg)
    return ok, lines


def export_image_bundle(
    root: Path,
    dest: Path,
    *,
    registry: str = DEFAULT_REGISTRY,
    tag: str = DEFAULT_TAG,
    names: tuple[str, ...] | None = None,
    build: bool = True,
    docker_bin: str = "docker",
) -> Path:
    """Save images to a tarball for manual transfer (air-gapped / offline)."""
    if build:
        build_images(root, registry=registry, tag=tag, names=names, docker_bin=docker_bin)
    refs = [image_ref(registry, img.repository, tag) for img in resolve_image_names(names, root)]
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [docker_bin, "save", "-o", str(dest), *refs],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker save failed")[:400])
    return dest


def smoke_test_images(
    root: Path,
    *,
    registry: str = DEFAULT_REGISTRY,
    tag: str = DEFAULT_TAG,
    names: tuple[str, ...] | None = None,
    docker_bin: str = "docker",
    on_log: LogFn | None = None,
) -> list[str]:
    """Run service-declared smoke checks against built images."""
    log = on_log or _log_default
    tested: list[str] = []
    for image in resolve_image_names(names, root):
        ref = image_ref(registry, image.repository, tag)
        for smoke in image.smoke_tests:
            command = [docker_bin, "run", "--rm"]
            if smoke.entrypoint:
                command.extend(["--entrypoint", smoke.entrypoint])
            command.extend([ref, *smoke.command])
            log(f"Testing {image.name}: {' '.join(shlex.quote(part) for part in smoke.command)}")
            proc = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            output = f"{proc.stdout}\n{proc.stderr}"
            if proc.returncode != 0:
                raise RuntimeError(f"Smoke test failed for {image.name}: {output.strip()[:400]}")
            if smoke.contains and smoke.contains not in output:
                raise RuntimeError(f"Smoke test for {image.name} did not contain expected text {smoke.contains!r}")
        tested.append(image.name)
        log(f"Passed {len(image.smoke_tests)} smoke test(s) for {image.name}")
    return tested


def audit_images(
    root: Path,
    *,
    names: tuple[str, ...] | None = None,
    on_log: LogFn | None = None,
) -> list[str]:
    """Run blocking dependency audits declared by service image contracts."""
    log = on_log or _log_default
    audited: list[str] = []
    for image in resolve_image_names(names, root):
        if image.requirements is None:
            log(f"No dependency audit declared for {image.name}")
            continue
        command = [sys.executable, "-m", "pip_audit", "-r", str(root.resolve() / image.requirements)]
        log(f"Auditing dependencies for {image.name}")
        proc = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout or "dependency audit failed")[:800]
            raise RuntimeError(f"Dependency audit failed for {image.name}: {output}")
        audited.append(image.name)
        log(f"Dependency audit passed for {image.name}")
    return audited
