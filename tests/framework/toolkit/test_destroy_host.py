from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.deploy.destructive_guard import (
    RecoveryCheckpointRequiredError,
    record_verified_checkpoint,
)
from toolkit.core.infra.infra_destroy import destroy_host_guarded


def _checkpoint(root: Path) -> None:
    evidence = root / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    record_verified_checkpoint(root, ["infra", "apps", "media"], [evidence])


def test_destroy_host_requires_checkpoint_before_destroy(tmp_path: Path) -> None:
    with patch("toolkit.core.infra.infra_destroy.destroy_infrastructure") as destroy:
        with pytest.raises(RecoveryCheckpointRequiredError):
            destroy_host_guarded(tmp_path)

    destroy.assert_not_called()


def test_destroy_host_preserves_state_when_lxc_destroy_fails(tmp_path: Path) -> None:
    _checkpoint(tmp_path)
    state = tmp_path / "infrastructure" / "terraform.tfstate"
    state.parent.mkdir()
    state.write_text("state")
    with (
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure", return_value=1),
        patch("toolkit.core.infra.infra_destroy.clean_tofu_state") as clean,
        patch("subprocess.run") as run,
    ):
        assert destroy_host_guarded(tmp_path) == 1

    clean.assert_not_called()
    run.assert_not_called()
    assert state.exists()


def test_destroy_host_preserves_state_when_zfs_wipe_fails(tmp_path: Path) -> None:
    _checkpoint(tmp_path)
    inventory = tmp_path / "automation" / "ansible" / "inventory" / "hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    with (
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure", return_value=0),
        patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_inventory.generated_extra_vars", return_value=[]),
        patch("subprocess.run", return_value=MagicMock(returncode=2, stderr="zfs failed")),
        patch("toolkit.core.infra.infra_destroy.clean_tofu_state") as clean,
    ):
        assert destroy_host_guarded(tmp_path) == 2

    clean.assert_not_called()


def test_destroy_host_cleans_state_only_after_verified_destroy_and_zfs(tmp_path: Path) -> None:
    _checkpoint(tmp_path)
    inventory = tmp_path / "automation" / "ansible" / "inventory" / "hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "old.conf").write_text("old")
    with (
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure", return_value=0),
        patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="ansible-playbook"),
        patch("toolkit.core.ansible.ansible_inventory.generated_extra_vars", return_value=[]),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
        patch("toolkit.core.infra.infra_destroy.clean_tofu_state", return_value=2) as clean,
    ):
        assert destroy_host_guarded(tmp_path) == 0

    clean.assert_called_once()
    assert list(generated.iterdir()) == []
