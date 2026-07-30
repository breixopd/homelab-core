"""Digest resolution and release construction for controlled updates."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from toolkit.core.ops.release_state import ReleaseState, build_release
from toolkit.core.ops.update_plan import UpdateCandidate

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.schema import NodeId

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseUpdateError(RuntimeError):
    """A target release could not be resolved or safely constructed."""


def resolve_target_digest(
    candidate: UpdateCandidate,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Resolve an image tag to its immutable multi-platform registry digest."""
    command = [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        candidate.target_image,
        "--format",
        "{{json .Manifest.Digest}}",
    ]
    try:
        result = run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseUpdateError(f"digest resolution failed for {candidate.service}") from exc
    if result.returncode != 0:
        raise ReleaseUpdateError(f"digest resolution failed for {candidate.service}")
    try:
        digest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseUpdateError(f"registry returned an invalid digest for {candidate.service}") from exc
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ReleaseUpdateError(f"registry returned an invalid digest for {candidate.service}")
    separator = candidate.target_image.rfind(":")
    if separator <= candidate.target_image.rfind("/"):
        raise ReleaseUpdateError(f"target image has no explicit tag for {candidate.service}")
    return f"{candidate.target_image[:separator]}@{digest}"


def build_updated_release(
    active: ReleaseState | None,
    resolved_images: dict[str, str],
    target_versions: dict[str, str],
    *,
    created_at: str,
) -> ReleaseState:
    images = dict(active.images) if active is not None else {}
    versions = dict(active.versions) if active is not None else {}
    images.update(resolved_images)
    versions.update(target_versions)
    return build_release(images, versions, created_at=created_at)


def affected_roles(root: Path, cfg: Config, services: set[str]) -> tuple[NodeId, ...]:
    """Return the enabled node roles whose generated model owns selected services."""
    from toolkit.core.generate.compose_assemble import assemble_compose_text, assemble_role_compose_text

    roles: list[NodeId] = []
    if not cfg.is_multi_node:
        document = yaml.safe_load(assemble_compose_text(root, cfg))
        declared = set(document.get("services", {})) if isinstance(document, dict) else set()
        if services <= declared:
            return (cast("NodeId", cfg.control_node),)
        raise ReleaseUpdateError("selected update service is not in the active Compose model")
    for role_name in cfg.enabled_nodes:
        role = cast("NodeId", role_name)
        document = yaml.safe_load(assemble_role_compose_text(root, cfg, role))
        declared = set(document.get("services", {})) if isinstance(document, dict) else set()
        if declared.intersection(services):
            roles.append(role)
    if not roles:
        raise ReleaseUpdateError("selected update services have no active node placement")
    return tuple(roles)


def selected_services_require_backup(root: Path, cfg: Config, services: set[str]) -> bool:
    """Return whether any selected runtime service belongs to a stateful manifest."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog(root)
    for manifest in catalog.manifests:
        if not manifest.stateful or not service_is_enabled(cfg, manifest):
            continue
        compose_path = root / "toolkit" / "services" / manifest.name / "compose.yaml"
        try:
            document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ReleaseUpdateError(f"cannot inspect storage ownership for {manifest.name}") from exc
        runtime_services = set(document.get("services", {})) if isinstance(document, dict) else set()
        if runtime_services.intersection(services):
            return True
    return False


def snapshot_update_roles(
    root: Path,
    cfg: Config,
    roles: tuple[NodeId, ...],
    *,
    actor: str,
    on_result: Callable[[NodeId, bool], None] | None = None,
) -> None:
    """Create encrypted pre-update snapshots and fail if any affected role fails."""
    callback = on_result or (lambda _role, _ok: None)
    remote = cfg.proxmox.provision_machines
    for role in roles:
        if remote:
            from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

            command = shlex.join(
                [
                    "/opt/homelab/.venv/bin/python3",
                    "-m",
                    "toolkit.cli",
                    "--root",
                    "/opt/homelab",
                    "maintenance",
                    "snapshot",
                    "--node",
                    role,
                ]
            )
            rc, _output, _error = ssh_run_on_vm(
                cfg,
                cfg.node_ip(role),
                command,
                root=root,
                timeout=3_900,
            )
            ok = rc == 0
        else:
            from toolkit.core.ops.backups import run_node_snapshot

            ok = run_node_snapshot(root, role, actor=actor).ok
        callback(role, ok)
        if not ok:
            raise ReleaseUpdateError(f"pre-update backup failed on {role}")
