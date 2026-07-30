"""Reconcile manifest-declared bind-mount ownership on a managed node."""

from __future__ import annotations

import os
from pathlib import Path


def _chown_tree(path: Path, uid: int, gid: int, logs: list[str], *, recursive: bool = True) -> None:
    try:
        os.chown(path, uid, gid)
        if recursive:
            for child in path.rglob("*"):
                if child.is_symlink():
                    continue
                try:
                    os.chown(child, uid, gid)
                except OSError:
                    pass
        logs.append(f"Fixed permissions on {path} ({uid}:{gid})")
    except OSError as exc:
        logs.append(f"Could not chown {path}: {exc}")


def fix_volume_permissions(root: Path, *, node: str) -> list[str]:
    """Create and chown only paths declared by enabled service manifests."""
    root = root.resolve()
    logs: list[str] = []

    from toolkit.core.config.config import Config, config_path, load_config
    from toolkit.core.config.storage import env_path, secrets_path
    from toolkit.core.generate.generate import _build_env_vars
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_storage_nodes
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.manifest.storage import read_role_environment
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    cfg = load_config(config_path(root)) if config_path(root).is_file() else Config()
    if node not in cfg.enabled_nodes:
        raise ValueError(f"cannot reconcile permissions for unknown or disabled node {node!r}")
    environment = read_role_environment(env_path(node, root))
    if not environment:
        secrets = load_secrets_plaintext(secrets_path(root)) if secrets_path(root).is_file() else {}
        environment = _build_env_vars(cfg, node, secrets, root)
    catalog_root = root if (root / "toolkit/services").is_dir() else None

    for manifest in load_service_catalog(catalog_root).manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        for asset in manifest.data_specs:
            if node not in manifest_storage_nodes(cfg, manifest, asset.runtime_service):
                continue
            if asset.source_env is None or (not asset.manage_permissions and not asset.host_subdirs):
                continue
            raw = environment.get(asset.source_env, "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            if asset.source_subpath:
                path /= asset.source_subpath
            path = path.resolve(strict=False)
            if path == Path("/"):
                raise ValueError(f"{manifest.name}.{asset.name} resolves to the filesystem root")
            path.mkdir(parents=True, exist_ok=True)
            for subdir in asset.host_subdirs:
                subdir_path = path / subdir
                subdir_path.mkdir(parents=True, exist_ok=True)
                if not asset.manage_permissions:
                    _chown_tree(subdir_path, asset.host_uid, asset.host_gid, logs, recursive=False)
            if asset.manage_permissions:
                _chown_tree(path, asset.host_uid, asset.host_gid, logs)

        for declared in manifest.host_paths if manifest_node(cfg, manifest) == node else ():
            path = root / declared.path
            if declared.create:
                path.mkdir(parents=True, exist_ok=True)
                for subdir in declared.subdirs:
                    (path / subdir).mkdir(parents=True, exist_ok=True)
            elif not path.exists():
                logs.append(f"Missing generated config path {path}")
                continue
            _chown_tree(path, declared.uid, declared.gid, logs, recursive=declared.recursive)
            try:
                path.chmod(int(declared.mode, 8))
                for subdir in declared.subdirs:
                    (path / subdir).chmod(int(declared.mode, 8))
            except OSError as exc:
                logs.append(f"Could not chmod {path}: {exc}")

    return logs
