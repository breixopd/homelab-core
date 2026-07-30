"""Dedicated, restricted SSH identity for an off-host Kopia repository."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True, slots=True)
class BackupSSHIdentity:
    private_key: Path
    public_path: Path
    public_key: str


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_bytes(content)
    pending.chmod(mode)
    pending.replace(path)
    path.chmod(mode)


def _load_identity(private_path: Path) -> tuple[Ed25519PrivateKey, str] | None:
    try:
        private = serialization.load_ssh_private_key(private_path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            return None
        public = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        return private, f"{public} homelab-kopia-backup"
    except (OSError, ValueError, TypeError):
        return None


def ensure_backup_ssh_identity(root: Path) -> BackupSSHIdentity:
    directory = root / "config" / "kopia"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    private_path = directory / "remote_ed25519"
    public_path = directory / "remote_ed25519.pub"
    loaded = _load_identity(private_path)
    if loaded is None:
        private = Ed25519PrivateKey.generate()
        _atomic_write(
            private_path,
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            ),
            0o600,
        )
        public = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        public_key = f"{public} homelab-kopia-backup"
    else:
        _private, public_key = loaded
    _atomic_write(public_path, f"{public_key}\n".encode(), 0o644)
    return BackupSSHIdentity(private_path, public_path, public_key)


def write_remote_known_hosts(root: Path, address: str, port: int) -> Path:
    """Copy a previously verified target host key into Kopia's isolated trust store."""
    inventory = root / "automation" / "ansible" / "inventory" / "known_hosts"
    if not inventory.is_file():
        raise RuntimeError("remote backup target has no verified SSH host key")
    target = address if port == 22 else f"[{address}]:{port}"
    lines = [line for line in inventory.read_text(encoding="utf-8").splitlines() if line.startswith(f"{target} ")]
    if not lines:
        try:
            result = subprocess.run(
                ["ssh-keygen", "-F", target, "-f", str(inventory)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
        except (OSError, subprocess.SubprocessError):
            lines = []
    if not lines:
        raise RuntimeError("remote backup target has no verified SSH host key")
    output = root / "config" / "kopia" / "known_hosts"
    output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output, ("\n".join(lines) + "\n").encode(), 0o600)
    return output
