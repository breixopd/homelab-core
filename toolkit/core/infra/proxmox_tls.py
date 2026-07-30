"""Authenticated Proxmox CA discovery and OpenTofu trust bundles."""

from __future__ import annotations

import os
import re
import ssl
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import certifi

from toolkit.core.deploy.destructive_guard import write_sensitive_file
from toolkit.core.infra.proxmox_ssh import build_proxmox_ssh_command

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
_REMOTE_CA_PATH = "/etc/pve/pve-root-ca.pem"
_SYSTEM_CA_BUNDLE_PATHS = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"),
    Path("/etc/ssl/ca-bundle.pem"),
    Path("/etc/ssl/cert.pem"),
)


def _validated_pem(data: str, *, source: str) -> str:
    match = _CERTIFICATE.search(data)
    if match is None:
        raise RuntimeError(f"{source} does not contain a PEM certificate")
    certificate = match.group(0) + "\n"
    try:
        ssl.PEM_cert_to_DER_cert(certificate)
    except ValueError as exc:
        raise RuntimeError(f"{source} contains an invalid PEM certificate") from exc
    return data.rstrip() + "\n"


def _system_ca_bundle() -> Path:
    paths = ssl.get_default_verify_paths()
    candidates = (
        os.environ.get(paths.openssl_cafile_env, ""),
        paths.cafile,
        paths.openssl_cafile,
        *map(str, _SYSTEM_CA_BUNDLE_PATHS),
        certifi.where(),
    )
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            return path
    raise RuntimeError("no CA bundle was found; install system CA certificates or set SSL_CERT_FILE")


def _configured_ca(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f"configured Proxmox CA file does not exist: {path}")
    return path


def _fetch_proxmox_ca(root: Path, cfg: Config) -> Path:
    trust_dir = root / ".homelab-state" / "trust"
    cached = trust_dir / "proxmox-ca.pem"
    cached_data = ""
    if cached.is_file():
        cached_data = _validated_pem(cached.read_text(encoding="utf-8"), source=str(cached))

    try:
        command = build_proxmox_ssh_command(cfg, root, f"cat {_REMOTE_CA_PATH}")
    except ValueError as exc:
        if cached_data:
            return cached
        raise RuntimeError(f"could not retrieve the Proxmox CA over SSH: {exc}") from exc
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=cfg.proxmox.ssh.connect_timeout + 15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if cached_data:
            return cached
        raise RuntimeError(f"could not retrieve the Proxmox CA over SSH: {exc}") from exc
    if result.returncode != 0:
        if cached_data:
            return cached
        detail = result.stderr.strip() or f"SSH exited with status {result.returncode}"
        raise RuntimeError(f"could not retrieve the Proxmox CA over SSH: {detail}")
    certificate = _validated_pem(result.stdout, source="Proxmox CA returned over SSH")
    if certificate != cached_data:
        write_sensitive_file(cached, certificate)
    return cached


def ensure_proxmox_ca_bundle(root: Path, cfg: Config) -> Path | None:
    """Return a system-plus-Proxmox CA bundle for verified provider API calls."""
    root = root.resolve()
    if not cfg.proxmox.api_url:
        return None
    private_ca = (
        _configured_ca(root, cfg.proxmox.tls_ca_file) if cfg.proxmox.tls_ca_file else _fetch_proxmox_ca(root, cfg)
    )
    private_data = _validated_pem(private_ca.read_text(encoding="utf-8"), source=str(private_ca))
    system_data = _system_ca_bundle().read_text(encoding="utf-8")
    bundle = root / ".homelab-state" / "trust" / "proxmox-ca-bundle.pem"
    write_sensitive_file(bundle, system_data.rstrip() + "\n" + private_data)
    return bundle
