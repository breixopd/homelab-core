from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    DesiredStateValidationError,
    create_machine,
    read_machines_view,
    remove_machine,
    update_machine,
)
from toolkit.controller.read_models import MachineCreate, MachineRemove, MachineUpdate
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.machines import MachineSpec


def _worker(*, enabled: bool = True, managed: bool = False) -> MachineSpec:
    return MachineSpec(
        enabled=enabled,
        managed=managed,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )


def test_machine_view_reports_templates_and_removal_impact(tmp_path: Path) -> None:
    save_config(Config(), config_path(tmp_path))

    view = read_machines_view(tmp_path)

    assert [template.template_id for template in view.templates] == ["apps", "infra", "media"]
    infra = next(machine for machine in view.machines if machine.machine_id == "infra")
    assert infra.spec.kind == "lxc"
    assert "control machine" in infra.removal_blockers
    assert infra.can_remove is False
    assert "caddy" in infra.services


def test_machine_view_uses_project_owned_service_catalog(monkeypatch, tmp_path: Path) -> None:
    from toolkit.core.manifest.catalog import load_service_catalog as real_load_service_catalog

    service_dir = tmp_path / "toolkit" / "services" / "custom-service"
    service_dir.mkdir(parents=True)
    (service_dir / "service.yaml").write_text("name: custom-service\n", encoding="utf-8")
    roots: list[Path | None] = []

    def load_service_catalog(root: Path | None = None):
        roots.append(root)
        return real_load_service_catalog()

    monkeypatch.setattr("toolkit.core.manifest.catalog.load_service_catalog", load_service_catalog)
    save_config(Config(), config_path(tmp_path))

    read_machines_view(tmp_path)

    assert roots == [tmp_path]


def test_machine_create_and_update_are_revision_guarded(tmp_path: Path) -> None:
    save_config(Config(), config_path(tmp_path))
    initial = read_machines_view(tmp_path)

    created = create_machine(
        tmp_path,
        MachineCreate(expected_revision=initial.revision, machine_id="worker-east", spec=_worker()),
    )
    assert any(machine.machine_id == "worker-east" for machine in created.machines)

    worker = next(machine for machine in created.machines if machine.machine_id == "worker-east")
    updated = update_machine(
        tmp_path,
        "worker-east",
        MachineUpdate(
            expected_revision=created.revision,
            spec=worker.spec.model_copy(update={"cores": 6}),
        ),
    )
    assert next(machine.spec.cores for machine in updated.machines if machine.machine_id == "worker-east") == 6

    with pytest.raises(DesiredStateConflictError):
        update_machine(
            tmp_path,
            "worker-east",
            MachineUpdate(expected_revision=created.revision, spec=worker.spec),
        )


def test_machine_removal_requires_disabled_unmanaged_unplaced_definition(tmp_path: Path) -> None:
    machines = {**Config().machines, "worker-east": _worker(enabled=False)}
    save_config(Config(machines=machines), config_path(tmp_path))
    view = read_machines_view(tmp_path)
    worker = next(machine for machine in view.machines if machine.machine_id == "worker-east")
    assert worker.can_remove is True

    removed = remove_machine(
        tmp_path,
        "worker-east",
        MachineRemove(
            expected_revision=view.revision,
            machine_id="worker-east",
            confirmation="worker-east",
        ),
    )
    assert all(machine.machine_id != "worker-east" for machine in removed.machines)


def test_machine_removal_rejects_managed_and_control_definitions(tmp_path: Path) -> None:
    machines = {
        **Config().machines,
        "worker-east": _worker(enabled=False, managed=True),
    }
    save_config(Config(machines=machines), config_path(tmp_path))
    view = read_machines_view(tmp_path)

    with pytest.raises(DesiredStateValidationError, match="approved retirement"):
        remove_machine(
            tmp_path,
            "worker-east",
            MachineRemove(
                expected_revision=view.revision,
                machine_id="worker-east",
                confirmation="worker-east",
            ),
        )

    with pytest.raises(DesiredStateValidationError, match="confirmation"):
        remove_machine(
            tmp_path,
            "infra",
            MachineRemove(
                expected_revision=view.revision,
                machine_id="infra",
                confirmation="wrong",
            ),
        )


def test_machine_view_exposes_retirement_eligibility_separately_from_definition_removal(tmp_path: Path) -> None:
    machines = {**Config().machines, "worker-east": _worker(managed=True)}
    save_config(Config(machines=machines), config_path(tmp_path))

    view = read_machines_view(tmp_path)

    worker = next(machine for machine in view.machines if machine.machine_id == "worker-east")
    apps = next(machine for machine in view.machines if machine.machine_id == "apps")
    assert worker.can_remove is False
    assert worker.can_retire is True
    assert worker.retirement_blockers == []
    assert apps.can_retire is False
    assert any("service" in blocker for blocker in apps.retirement_blockers)


@pytest.mark.parametrize(
    "update",
    (
        {"enabled": False},
        {"managed": False},
        {
            "kind": "vm",
            "admin_user": "debian",
            "cloud_image_datastore": "local",
            "cloud_image_format": "qcow2",
            "cloud_image_url": "https://images.example.test/debian.qcow2",
            "cloud_image_sha256": "a" * 64,
        },
        {"vmid": 999},
    ),
)
def test_ordinary_update_cannot_implicitly_replace_or_remove_a_managed_machine(tmp_path: Path, update) -> None:
    save_config(Config(), config_path(tmp_path))
    view = read_machines_view(tmp_path)
    apps = next(machine for machine in view.machines if machine.machine_id == "apps")

    with pytest.raises(DesiredStateValidationError, match="approved retirement or replacement"):
        update_machine(
            tmp_path,
            "apps",
            MachineUpdate(
                expected_revision=view.revision,
                spec=apps.spec.model_copy(update=update),
            ),
        )
