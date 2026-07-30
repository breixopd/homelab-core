"""OpenTofu environment helpers (API token from secrets)."""

from __future__ import annotations

import os
from pathlib import Path


def tofu_env_from_secrets(
    secrets: dict[str, str],
    *,
    allow_destroy: bool = False,
    root: Path | None = None,
    ca_bundle: Path | None = None,
) -> dict[str, str]:
    """Build environment for tofu CLI from homelab secrets."""
    env = os.environ.copy()
    if root is not None:
        root = root.resolve()
        env["HOMELAB_ROOT"] = str(root)
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        venv_bin = root / ".venv" / "bin"
        if venv_bin.is_dir():
            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        ansible_cfg = root / "automation" / "ansible" / "ansible.cfg"
        if ansible_cfg.is_file():
            env["ANSIBLE_CONFIG"] = str(ansible_cfg)
    token_id = secrets.get("PROXMOX_API_TOKEN_ID", "").strip()
    token_secret = secrets.get("PROXMOX_API_TOKEN_SECRET", "").strip()
    if token_id and token_secret:
        env["TF_VAR_proxmox_api_token"] = f"{token_id}={token_secret}"
        env["TF_VAR_proxmox_api_token_id"] = token_id
        env["TF_VAR_proxmox_api_token_secret"] = token_secret
    if allow_destroy:
        env["TF_VAR_allow_destroy"] = "true"
    if ca_bundle is not None:
        env["SSL_CERT_FILE"] = str(ca_bundle)
    return env


def load_tofu_env(root: Path, *, allow_destroy: bool = False) -> dict[str, str]:
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.infra.proxmox_tls import ensure_proxmox_ca_bundle
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    cfg = load_config(config_path(root))
    ca_bundle = ensure_proxmox_ca_bundle(root, cfg)
    return tofu_env_from_secrets(
        secrets,
        allow_destroy=allow_destroy,
        root=root,
        ca_bundle=ca_bundle,
    )
