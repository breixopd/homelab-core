"""Shared media-library service plugin."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class MediaLibraryPlugin(ServicePlugin):
    service = "media-library"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Require the declared library layout and a reversible write probe."""
        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
        from toolkit.core.manifest.variables import compile_manifest_host_sources
        from toolkit.services.sdk import VerifyCheck, ssh_on_vm

        sources = compile_manifest_host_sources(cfg, self.manifest, DEFAULT_HOMELAB_ROOT)
        library_root = sources["MEDIA_LIBRARY_ROOT"]
        quoted_root = shlex.quote(library_root)
        command = (
            f"set -eu; test -d {quoted_root}; "
            f"for directory in tv movies music downloads; do test -d {quoted_root}/$directory; done; "
            f'probe={quoted_root}/.homelab-verify-$$; : > "$probe"; rm -f "$probe"'
        )
        rc, out, err = ssh_on_vm(cfg, vm_ip, command, root=root, timeout=20)
        return [
            VerifyCheck(
                self.service,
                "layout_writable",
                rc == 0,
                f"{library_root}: required directories writable" if rc == 0 else (err or out or "probe failed")[:120],
            )
        ]
