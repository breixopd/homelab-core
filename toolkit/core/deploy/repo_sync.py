"""Synchronize the homelab repository tree to managed machines."""

from __future__ import annotations

import secrets
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from toolkit.core.ansible.ansible_ssh import scp_to_vm, ssh_run_on_vm
from toolkit.core.config.config import Config, config_path, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, env_path, hook_bundle_path

DEFAULT_SYNC_PATHS: tuple[str, ...] = (
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "toolkit",
    "scripts",
    "automation",
    "infrastructure",
    "docker-compose.yml",
    "stacks",
    "config.yaml",
    "generated",
    "config",
    ".homelab-state/trust/proxmox-ca.pem",
)

# Relative path (under repo_dest) where the controller's HEAD commit SHA is
# stamped after each sync so verify_repo_parity can detect drift.
STAMP_REL = ".homelab-state/commit-sha"


def controller_commit_sha(root: Path) -> str | None:
    """Return a stable controller source revision.

    Git HEAD is preferred for normal operator checkouts. Production controller
    bind mounts intentionally omit ``.git``; there we hash the same
    allow-listed source tree used for guest synchronization.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    root = root.resolve()
    digest = sha256()
    found = False
    for relative in DEFAULT_SYNC_PATHS:
        source = root / relative
        if source.is_symlink():
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0link\0")
            digest.update(str(source.readlink()).encode("utf-8"))
            found = True
            continue
        paths = (source,) if source.is_file() else tuple(sorted(path for path in source.rglob("*") if path.is_file()))
        for path in paths:
            path_relative = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or ".pytest_cache" in path.parts:
                continue
            try:
                digest.update(path_relative.encode("utf-8"))
                digest.update(b"\0")
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
            except OSError:
                continue
            found = True
    return digest.hexdigest() if found else None


def _stamp_commit_on_guest(
    cfg: Config,
    root: Path,
    vm_ip: str,
    sha: str,
    repo_dest: str,
) -> None:
    """Write the controller's commit SHA to .homelab-state/commit-sha on the guest.

    Best-effort: a stamping failure does not abort the sync, but the next
    parity verify will report a missing stamp for this guest.
    """
    state_dir = f"{repo_dest}/.homelab-state"
    stamp_file = f"{state_dir}/{STAMP_REL.split('/', 1)[1]}"
    cmd = f"mkdir -p {shlex.quote(state_dir)} && printf %s {shlex.quote(sha)} > {shlex.quote(stamp_file)}"
    ssh_run_on_vm(cfg, vm_ip, cmd, root=root, timeout=30)


def _build_tarball(
    root: Path,
    paths: tuple[str, ...] = DEFAULT_SYNC_PATHS,
    *,
    node: str | None = None,
    machine_ids: tuple[str, ...] = (),
    control_node: str = "",
    generated_artifact_nodes: Mapping[str, str | tuple[str, ...]] | None = None,
    config_source_nodes: Mapping[str, tuple[str, bool]] | None = None,
) -> Path:
    root = root.resolve()
    tmp = Path(tempfile.mkdtemp(prefix="homelab-sync-"))
    archive = tmp / "homelab-sync.tgz"

    candidates = machine_ids or (
        tuple(path.name for path in (root / "generated").iterdir() if path.is_dir() and path.name != "bundles")
        if (root / "generated").is_dir()
        else ()
    )
    artifact_nodes = {
        path: (owners,) if isinstance(owners, str) else tuple(owners)
        for path, owners in (generated_artifact_nodes or {}).items()
    }
    config_sources = dict(config_source_nodes or {})

    def generated_member_allowed(name: str) -> bool:
        if name == "generated":
            return True
        if not name.startswith("generated/"):
            return True
        if name == "generated/bundles" or name.startswith("generated/bundles/"):
            return False

        if name in artifact_nodes:
            owners = artifact_nodes[name]
            return node == control_node or node in owners

        for candidate in candidates:
            prefix = f"generated/{candidate}"
            if name == prefix or name.startswith(f"{prefix}/"):
                return node == control_node or candidate == node

        selected_artifacts = {path for path, owners in artifact_nodes.items() if node == control_node or node in owners}
        directory_prefix = name.rstrip("/") + "/"
        return any(path.startswith(directory_prefix) for path in selected_artifacts)

    def safe_member(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = info.name.rstrip("/")
        if node and name == "docker-compose.yml" and node != control_node:
            return None
        if node and not generated_member_allowed(name):
            return None
        if node and name.startswith("config/"):
            owners = [
                (owner_node, enabled)
                for source_path, (owner_node, enabled) in config_sources.items()
                if name == source_path or name.startswith(f"{source_path.rstrip('/')}/")
            ]
            if owners and not any(
                node == control_node or (enabled and owner_node == node) for owner_node, enabled in owners
            ):
                return None
        if name.startswith("generated/"):
            basename = Path(name).name
            if name not in artifact_nodes and (
                basename in {".env", ".hooks.env"} or basename.startswith((".env.", ".hooks.env."))
            ):
                return None
        return info

    with tarfile.open(archive, "w:gz") as tar:
        for name in paths:
            src = root / name
            if src.exists():
                tar.add(src, arcname=name, filter=safe_member)
        if node:
            selected = candidates if node == control_node else (node,)
            for candidate in selected:
                runtime = env_path(candidate, root)
                if not runtime.is_file():
                    raise FileNotFoundError(f"Node-scoped Compose environment missing: {runtime}")
                bundle = hook_bundle_path(candidate, root)
                if not bundle.is_file():
                    raise FileNotFoundError(f"Node-scoped hook bundle missing: {bundle}")
                tar.add(runtime, arcname=f"generated/{candidate}/.env")
                hook_target = (
                    f"generated/bundles/{candidate}/.hooks.env"
                    if node == control_node
                    else f"generated/{candidate}/.hooks.env"
                )
                tar.add(bundle, arcname=hook_target)
                if node == control_node and candidate == node:
                    tar.add(bundle, arcname=f"generated/{candidate}/.hooks.env")
    return archive


def sync_repo_to_guest(
    root: Path,
    cfg: Config,
    vm_ip: str,
    *,
    repo_dest: str = DEFAULT_HOMELAB_ROOT,
    paths: tuple[str, ...] = DEFAULT_SYNC_PATHS,
) -> None:
    """Tarball selected repo paths, scp to guest, extract under repo_dest.

    After extraction, stamps the controller's current HEAD commit SHA to
    ``<repo_dest>/.homelab-state/commit-sha`` so that ``verify_repo_parity``
    can detect drift on subsequent deploys.
    """
    node = next((name for name in cfg.enabled_nodes if cfg.node_ip(name) == vm_ip), None)
    if node is None:
        raise ValueError(f"No enabled node matches guest IP {vm_ip}")
    from toolkit.core.manifest.artifacts import compile_config_sources, compile_generated_artifacts
    from toolkit.core.manifest.catalog import load_service_catalog

    enabled_nodes = tuple(cfg.enabled_nodes)
    catalog = load_service_catalog(root)
    compiled_artifacts = compile_generated_artifacts(cfg, catalog, root)
    generated_artifact_nodes: dict[str, set[str]] = {}
    all_artifact_nodes: dict[str, set[str]] = {}
    artifact_enabled: dict[str, bool] = {}
    for artifact in compiled_artifacts:
        all_artifact_nodes.setdefault(artifact.source_path, set()).add(artifact.node)
        artifact_enabled[artifact.source_path] = artifact_enabled.get(artifact.source_path, False) or artifact.enabled
        if artifact.enabled:
            generated_artifact_nodes.setdefault(artifact.source_path, set()).add(artifact.node)
    config_source_nodes = {
        source.path: (source.node, source.enabled) for source in compile_config_sources(cfg, catalog, root)
    }
    from toolkit.core.manifest.ownership import current_ownership, ownership_tombstones

    removed_machine_ids = ownership_tombstones(root, current_ownership(cfg, catalog)).machines
    archive = _build_tarball(
        root,
        paths,
        node=node,
        machine_ids=enabled_nodes,
        control_node=cfg.control_node,
        generated_artifact_nodes={path: tuple(sorted(nodes)) for path, nodes in generated_artifact_nodes.items()},
        config_source_nodes=config_source_nodes,
    )
    sha = controller_commit_sha(root)
    try:
        remote_archive = f"/root/.homelab-sync-{secrets.token_hex(12)}.tgz"
        scp_to_vm(cfg, root, archive, vm_ip, remote_archive)
        quoted_archive = shlex.quote(remote_archive)
        quoted_dest = shlex.quote(repo_dest)
        role_generated = shlex.quote(f"{repo_dest}/generated/{node}")
        generated_root = shlex.quote(f"{repo_dest}/generated")
        stale_bundles = shlex.quote(f"{repo_dest}/generated/bundles")
        workload_cleanup = ""
        if node != cfg.control_node:
            retained_generated_roots = sorted(
                {
                    relative.parts[0]
                    for source_path, owners in generated_artifact_nodes.items()
                    if node in owners
                    and source_path.startswith("generated/")
                    and (relative := Path(source_path).relative_to("generated")).parts
                }
            )
            retained_names = (node, *retained_generated_roots)
            exclusions = " ".join(f"! -name {shlex.quote(name)}" for name in retained_names)
            workload_cleanup = f"find {generated_root} -mindepth 1 -maxdepth 1 {exclusions} -exec rm -rf -- {{}} +; "
        extract = (
            "set -e; "
            f"archive={quoted_archive}; trap 'rm -f \"$archive\"' EXIT; "
            f"mkdir -p {quoted_dest} {role_generated}; "
            f"{workload_cleanup}"
            f"find {role_generated} -maxdepth 1 -type f "
            "\\( -name '.env.*' -o -name '.hooks.env.*' \\) -delete 2>/dev/null || true; "
            f"rm -rf {stale_bundles}; "
            f'tar xzf "$archive" --no-same-owner --no-overwrite-dir -C {quoted_dest} && '
            # Remove any stale .git/ — tarball sync doesn't ship .git, so a
            # leftover one from a manual clone causes repo-parity false positives.
            f"rm -rf {shlex.quote(f'{repo_dest}/.git')}"
        )
        stale_models: list[str] = []
        if node == cfg.control_node:
            stale_models.extend(f"{repo_dest}/generated/{candidate}" for candidate in removed_machine_ids)
            stale_models.extend(f"{repo_dest}/generated/bundles/{candidate}" for candidate in removed_machine_ids)
            for candidate in cfg.machines:
                if candidate in enabled_nodes:
                    continue
                stale_models.extend(
                    (
                        f"{repo_dest}/generated/{candidate}/compose.yaml",
                        f"{repo_dest}/generated/{candidate}/.env",
                        f"{repo_dest}/generated/{candidate}/.hooks.env",
                        f"{repo_dest}/generated/bundles/{candidate}/.hooks.env",
                    )
                )
        else:
            stale_models.extend(
                f"{repo_dest}/generated/{candidate}/compose.yaml" for candidate in cfg.machines if candidate != node
            )
            stale_models.extend(
                f"{repo_dest}/generated/{candidate}/.env" for candidate in cfg.machines if candidate != node
            )
            stale_models.extend(
                f"{repo_dest}/generated/{candidate}/.hooks.env" for candidate in cfg.machines if candidate != node
            )
        if not stale_models:
            stale_models.append(f"{repo_dest}/generated/.sync-noop")
        if node != cfg.control_node:
            stale_models.append(f"{repo_dest}/docker-compose.yml")
        stale_config = [
            f"{repo_dest}/{source_path}"
            for source_path, (owner_node, enabled) in config_source_nodes.items()
            if node != cfg.control_node and (not enabled or owner_node != node)
        ]
        stale_generated_artifacts = [
            f"{repo_dest}/{source_path}"
            for source_path, owners in all_artifact_nodes.items()
            if not artifact_enabled[source_path] or (node != cfg.control_node and node not in owners)
        ]
        extract += " && rm -f " + " ".join(shlex.quote(path) for path in stale_models)
        if stale_config:
            extract += " && rm -rf -- " + " ".join(shlex.quote(path) for path in stale_config)
        if stale_generated_artifacts:
            extract += " && rm -rf -- " + " ".join(shlex.quote(path) for path in stale_generated_artifacts)
        rc, _out, err = ssh_run_on_vm(cfg, vm_ip, extract, root=root, timeout=120)
        if rc != 0:
            raise RuntimeError(err.strip() or f"extract failed (exit {rc})")
        if sha:
            _stamp_commit_on_guest(cfg, root, vm_ip, sha, repo_dest)
    finally:
        archive.unlink(missing_ok=True)
        try:
            archive.parent.rmdir()
        except OSError:
            pass


def sync_repo_to_role(root: Path, role: str, *, repo_dest: str = DEFAULT_HOMELAB_ROOT) -> None:
    """Sync the repository to a configured machine by node ID."""
    cfg = load_config(config_path(root))
    try:
        ip = cfg.node_ip(role)
    except KeyError as exc:
        raise ValueError(f"Unknown or disabled machine: {role}") from exc
    sync_repo_to_guest(root, cfg, ip, repo_dest=repo_dest)
