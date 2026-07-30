"""Push homelab tree to a guest when Ansible synchronize is slow or hangs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from toolkit.core.config.config import config_path, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
from toolkit.core.deploy.repo_parity import (
    ParityResult,
    format_parity_report,
    verify_repo_parity,
)
from toolkit.core.deploy.repo_sync import sync_repo_to_guest

__all__ = [
    "ParityResult",
    "format_parity_report",
    "sync_guest_via_tar",
    "verify_repo_parity",
]


def sync_guest_via_tar(root: Path, vm_ip: str, *, repo_dest: str = DEFAULT_HOMELAB_ROOT) -> None:
    """Rsync-free fallback: tar on controller, scp + extract on guest."""
    cfg = load_config(config_path(root))
    sync_repo_to_guest(root, cfg, vm_ip, repo_dest=repo_dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tarball sync homelab tree to guest")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--vm-ip", required=True)
    parser.add_argument("--repo-dest", default=DEFAULT_HOMELAB_ROOT)
    args = parser.parse_args(argv)
    try:
        sync_guest_via_tar(args.root, args.vm_ip, repo_dest=args.repo_dest)
    except (OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
