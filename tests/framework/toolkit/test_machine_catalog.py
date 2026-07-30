from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.config.config import Config
from toolkit.core.machines import MachineSpec, load_default_machines, load_machine_templates


def test_builtin_machine_plugins_are_strict_and_discoverable() -> None:
    machines = load_default_machines()

    assert set(machines) == {"infra", "media", "apps"}
    assert machines["infra"].public_bridge == "vmbr0"
    assert machines["media"].data_disks[0].path == "/data"


def test_builtin_machine_catalog_returns_an_independent_mapping() -> None:
    first = load_default_machines()
    first.pop("apps")

    assert "apps" in load_default_machines()


def test_project_machine_templates_are_discovered_without_framework_edits(tmp_path) -> None:
    template = tmp_path / "machines" / "edge"
    template.mkdir(parents=True)
    template.joinpath("machine.yaml").write_text(
        "hostname: edge-01\naddress: 10.10.10.30\ngateway: 10.10.10.1\nvmid: 830\nlabels: [edge]\n",
        encoding="utf-8",
    )

    templates = load_machine_templates(tmp_path)

    assert templates["edge"].hostname == "edge-01"
    assert set(templates) == {"apps", "edge", "infra", "media"}


def test_project_machine_templates_cannot_shadow_packaged_defaults(tmp_path) -> None:
    template = tmp_path / "machines" / "infra"
    template.mkdir(parents=True)
    template.joinpath("machine.yaml").write_text(
        "hostname: duplicate\naddress: 10.10.10.30\ngateway: 10.10.10.1\nvmid: 830\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate packaged IDs"):
        load_machine_templates(tmp_path)


def test_config_accepts_arbitrary_machine_ids_without_role_literals() -> None:
    worker = MachineSpec(
        kind="lxc",
        hostname="worker-01",
        address="10.20.30.40",
        gateway="10.20.30.1",
        vmid=910,
        labels=("control", "compute"),
    )
    cfg = Config(machines={"worker-east": worker})

    assert cfg.enabled_nodes == ["worker-east"]
    assert cfg.node_ip("worker-east") == "10.20.30.40"


def test_machine_inventory_rejects_duplicate_addresses_and_vmids() -> None:
    machine = MachineSpec(hostname="one", address="10.0.0.10", gateway="10.0.0.1", vmid=900)

    with pytest.raises(ValidationError, match="machine VMIDs must be unique"):
        Config(machines={"one": machine, "two": machine.model_copy(update={"hostname": "two"})})

    with pytest.raises(ValidationError, match="machine addresses must be unique"):
        Config(
            machines={
                "one": machine,
                "two": machine.model_copy(update={"hostname": "two", "vmid": 901}),
            }
        )


def test_unknown_machine_lookup_fails_closed() -> None:
    cfg = Config()

    with pytest.raises(KeyError, match="unknown machine"):
        cfg.node_ip("not-declared")


def test_config_requires_exactly_one_enabled_control_machine() -> None:
    machine = MachineSpec(hostname="worker", address="10.0.0.10", gateway="10.0.0.1", vmid=900)

    with pytest.raises(ValidationError, match="exactly one enabled machine must have the control label"):
        Config(machines={"worker": machine})

    with pytest.raises(ValidationError, match="exactly one enabled machine must have the control label"):
        Config(
            machines={
                "one": machine.model_copy(update={"labels": ("control",)}),
                "two": machine.model_copy(
                    update={
                        "hostname": "worker-two",
                        "address": "10.0.0.11",
                        "vmid": 901,
                        "labels": ("control",),
                    }
                ),
            }
        )


def test_machine_rejects_root_mount_and_off_subnet_address() -> None:
    from toolkit.core.machines import MachineDisk

    with pytest.raises(ValidationError, match="data disk path cannot be root"):
        MachineDisk(path="/", size_gb=20)

    with pytest.raises(ValidationError, match="address and gateway must share the configured subnet"):
        MachineSpec(hostname="worker", address="10.0.1.10", gateway="10.0.0.1", cidr=24, vmid=900)


def test_managed_vm_requires_plugin_owned_admin_and_pinned_image() -> None:
    base = {
        "kind": "vm",
        "hostname": "worker-vm",
        "address": "10.0.0.10",
        "gateway": "10.0.0.1",
        "vmid": 900,
        "cloud_image_url": "https://images.example.test/debian.qcow2",
        "cloud_image_datastore": "local",
        "cloud_image_format": "qcow2",
    }

    with pytest.raises(ValidationError, match="admin_user"):
        MachineSpec.model_validate(base)
    with pytest.raises(ValidationError, match="cloud_image_sha256"):
        MachineSpec.model_validate({**base, "admin_user": "debian"})

    machine = MachineSpec.model_validate(
        {
            **base,
            "admin_user": "debian",
            "cloud_image_sha256": "a" * 64,
            "ssh_port": 2222,
        }
    )
    assert machine.effective_ssh_user == "debian"
    assert machine.ssh_port == 2222


def test_lxc_defaults_to_root_ssh_and_rejects_vm_image_fields() -> None:
    machine = MachineSpec(hostname="worker", address="10.0.0.10", gateway="10.0.0.1", vmid=900)
    assert machine.effective_ssh_user == "root"

    with pytest.raises(ValidationError, match="only valid for VM machines"):
        MachineSpec(
            hostname="worker",
            address="10.0.0.10",
            gateway="10.0.0.1",
            vmid=900,
            admin_user="debian",
        )


def test_managed_vm_requires_an_import_datastore_and_image_format() -> None:
    base = {
        "kind": "vm",
        "hostname": "worker-vm",
        "address": "10.0.0.10",
        "gateway": "10.0.0.1",
        "vmid": 900,
        "admin_user": "debian",
        "cloud_image_url": "https://images.example.test/debian.qcow2",
        "cloud_image_sha256": "a" * 64,
    }

    with pytest.raises(ValidationError, match="cloud_image_datastore"):
        MachineSpec.model_validate(base)
    with pytest.raises(ValidationError, match="cloud_image_format"):
        MachineSpec.model_validate({**base, "cloud_image_datastore": "local"})

    machine = MachineSpec.model_validate({**base, "cloud_image_datastore": "local", "cloud_image_format": "raw"})
    assert machine.cloud_image_format == "raw"
