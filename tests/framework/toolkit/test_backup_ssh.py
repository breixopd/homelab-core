from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.ops.backup_ssh import ensure_backup_ssh_identity, write_remote_known_hosts


def test_backup_ssh_identity_is_stable_and_private(tmp_path: Path) -> None:
    first = ensure_backup_ssh_identity(tmp_path)
    second = ensure_backup_ssh_identity(tmp_path)

    assert first == second
    assert first.private_key.stat().st_mode & 0o777 == 0o600
    assert first.public_key.startswith("ssh-ed25519 ")
    assert first.public_path.read_text(encoding="utf-8").strip() == first.public_key


def test_remote_known_hosts_copies_only_pinned_target(tmp_path: Path) -> None:
    inventory = tmp_path / "automation" / "ansible" / "inventory" / "known_hosts"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        "10.10.10.20 ssh-ed25519 AAAAremote\n10.10.10.30 ssh-ed25519 AAAAother\n",
        encoding="utf-8",
    )

    output = write_remote_known_hosts(tmp_path, "10.10.10.20", 22)

    assert output.read_text(encoding="utf-8") == "10.10.10.20 ssh-ed25519 AAAAremote\n"


def test_remote_known_hosts_requires_previously_verified_target(tmp_path: Path) -> None:
    inventory = tmp_path / "automation" / "ansible" / "inventory" / "known_hosts"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("10.10.10.30 ssh-ed25519 AAAAother\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="verified SSH host key"):
        write_remote_known_hosts(tmp_path, "10.10.10.20", 22)
