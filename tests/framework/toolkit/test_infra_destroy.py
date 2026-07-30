from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.destructive_guard import ResourcesStillPresentError
from toolkit.core.infra.infra_destroy import (
    _machine_tofu_targets,
    destroy_infrastructure,
    retire_machine_infrastructure,
    verify_proxmox_absence,
    verify_proxmox_machine_absence,
)
from toolkit.core.machines import MachineSpec


def test_destroy_infrastructure_missing_dir(tmp_path: Path):
    logs: list[str] = []
    rc = destroy_infrastructure(tmp_path, on_log=logs.append)
    assert rc == 1
    assert any("not found" in line for line in logs)


def test_destroy_infrastructure_no_tofu(tmp_path: Path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    logs: list[str] = []

    with (
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root"),
        patch("shutil.which", return_value=None),
    ):
        rc = destroy_infrastructure(tmp_path, on_log=logs.append)

    assert rc == 1
    assert any("not found on PATH" in line for line in logs)


def test_destroy_infrastructure_runs_destroy(tmp_path: Path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / ".terraform").mkdir()
    logs: list[str] = []

    destroy = MagicMock(returncode=0, stdout="destroyed", stderr="")
    show = MagicMock(returncode=0, stdout='{"values":{"root_module":{"resources":[]}}}', stderr="")
    with (
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root"),
        patch("shutil.which", return_value="/usr/bin/tofu"),
        patch("toolkit.core.infra.infra_env.load_tofu_env", return_value={}),
        patch("toolkit.core.infra.infra_destroy.verify_proxmox_absence"),
        patch("subprocess.run", side_effect=[destroy, show]) as mock_run,
    ):
        rc = destroy_infrastructure(tmp_path, on_log=logs.append)

    assert rc == 0
    assert mock_run.call_args_list[0].args[0][:2] == ["/usr/bin/tofu", "destroy"]
    assert mock_run.call_args_list[1].args[0][:3] == ["/usr/bin/tofu", "show", "-json"]


def test_destroy_fails_when_proxmox_inventory_cannot_prove_absence(tmp_path: Path):
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / ".terraform").mkdir()
    destroy = MagicMock(returncode=0, stdout="destroyed", stderr="")
    show = MagicMock(returncode=0, stdout='{"values":{"root_module":{"resources":[]}}}', stderr="")

    with (
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root"),
        patch("shutil.which", return_value="/usr/bin/tofu"),
        patch("toolkit.core.infra.infra_env.load_tofu_env", return_value={}),
        patch("subprocess.run", side_effect=[destroy, show]),
        patch(
            "toolkit.core.infra.infra_destroy.verify_proxmox_absence",
            side_effect=RuntimeError("inventory unavailable"),
        ),
    ):
        rc = destroy_infrastructure(tmp_path)

    assert rc == 1


def test_destroy_rejects_any_remaining_guest_resource_in_tofu_state(tmp_path: Path) -> None:
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / ".terraform").mkdir()
    logs: list[str] = []
    destroy = MagicMock(returncode=0, stdout="destroyed", stderr="")
    show = MagicMock(
        returncode=0,
        stdout=(
            '{"values":{"root_module":{"resources":['
            '{"address":"proxmox_virtual_environment_vm.machine[\\"renamed\\"]",'
            '"type":"proxmox_virtual_environment_vm","values":{}}]}}}'
        ),
        stderr="",
    )

    with (
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root"),
        patch("shutil.which", return_value="/usr/bin/tofu"),
        patch("toolkit.core.infra.infra_env.load_tofu_env", return_value={}),
        patch("toolkit.core.infra.infra_destroy.verify_proxmox_absence"),
        patch("subprocess.run", side_effect=[destroy, show]),
    ):
        rc = destroy_infrastructure(tmp_path, on_log=logs.append)

    assert rc == 1
    assert any("still tracks managed guest resources" in line for line in logs)


def test_retire_machine_targets_only_its_tofu_resources(tmp_path: Path) -> None:
    worker = MachineSpec(
        managed=True,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(tmp_path))
    infra = tmp_path / "infrastructure"
    (infra / ".terraform").mkdir(parents=True)
    destroyed = MagicMock(returncode=0, stdout="destroyed", stderr="")
    state = MagicMock(returncode=0, stdout='proxmox_virtual_environment_container.machine["infra"]\n', stderr="")

    with (
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root"),
        patch("shutil.which", return_value="/usr/bin/tofu"),
        patch("toolkit.core.infra.infra_env.load_tofu_env", return_value={}),
        patch("toolkit.core.infra.infra_destroy.verify_proxmox_machine_absence") as verify,
        patch("subprocess.run", side_effect=[destroyed, state]) as run,
    ):
        rc = retire_machine_infrastructure(tmp_path, "worker-east")

    assert rc == 0
    command = run.call_args_list[0].args[0]
    assert command[:4] == ["/usr/bin/tofu", "destroy", "-input=false", "-auto-approve"]
    assert '-target=proxmox_virtual_environment_container.machine["worker-east"]' in command
    assert '-target=random_password.machine_root["worker-east"]' in command
    assert all("infra" not in argument for argument in command[4:])
    verify.assert_called_once_with(tmp_path, "worker-east")


def test_vm_retirement_targets_guest_password_and_machine_scoped_image() -> None:
    assert _machine_tofu_targets("worker-east", "vm") == (
        'proxmox_virtual_environment_vm.machine["worker-east"]',
        'random_password.machine_root["worker-east"]',
        'proxmox_download_file.vm_image["worker-east"]',
    )


def test_machine_absence_verification_tolerates_malformed_unrelated_vm_inventory(tmp_path: Path) -> None:
    worker = MachineSpec(
        kind="vm",
        managed=True,
        hostname="worker-vm-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
        admin_user="debian",
        cloud_image_datastore="local",
        cloud_image_format="qcow2",
        cloud_image_url="https://images.example.test/debian.qcow2",
        cloud_image_sha256="a" * 64,
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(tmp_path))

    with (
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"PROXMOX_API_TOKEN_ID": "root@pam!test", "PROXMOX_API_TOKEN_SECRET": "secret"},
        ),
        patch("toolkit.core.infra.proxmox_tls.ensure_proxmox_ca_bundle", return_value=tmp_path / "ca.pem"),
        patch(
            "toolkit.core.infra.proxmox.list_proxmox_vms",
            return_value=[{"name": None, "vmid": None}, {"name": "other-vm", "vmid": 900}],
        ) as inventory,
    ):
        verify_proxmox_machine_absence(tmp_path, "worker-east")

    inventory.assert_called_once()


def test_full_absence_verification_checks_lxc_and_vm_inventory(tmp_path: Path) -> None:
    worker = MachineSpec(
        kind="vm",
        managed=True,
        hostname="worker-vm-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
        admin_user="debian",
        cloud_image_datastore="local",
        cloud_image_format="qcow2",
        cloud_image_url="https://images.example.test/debian.qcow2",
        cloud_image_sha256="a" * 64,
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(tmp_path))

    with (
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"PROXMOX_API_TOKEN_ID": "root@pam!test", "PROXMOX_API_TOKEN_SECRET": "secret"},
        ),
        patch("toolkit.core.infra.proxmox_tls.ensure_proxmox_ca_bundle", return_value=tmp_path / "ca.pem"),
        patch("toolkit.core.infra.proxmox.list_proxmox_lxcs", return_value=[]),
        patch(
            "toolkit.core.infra.proxmox.list_proxmox_vms",
            return_value=[{"name": "externally-renamed-worker", "vmid": 820}],
        ),
    ):
        with pytest.raises(ResourcesStillPresentError, match="VMID 820"):
            verify_proxmox_absence(tmp_path)
