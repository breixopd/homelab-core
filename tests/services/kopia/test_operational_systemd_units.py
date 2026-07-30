from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PLAYBOOK = ROOT / "automation" / "ansible" / "playbooks" / "bootstrap-lxc.yml"
DEPLOY_PLAYBOOK = PLAYBOOK.with_name("deploy-server-toolkit.yml")
RECOVERY_PLAYBOOK = PLAYBOOK.with_name("deploy-recover.yml")
CORE_UNITS_TASKS = ROOT / "automation" / "ansible" / "tasks" / "reconcile-operational-units.yml"
KOPIA_UNITS_TASKS = ROOT / "toolkit" / "services" / "kopia" / "ansible" / "host.yml"


def test_operational_units_reconcile_on_every_guest_deployment_path() -> None:
    include = "../tasks/reconcile-operational-units.yml"

    assert include in PLAYBOOK.read_text(encoding="utf-8")
    assert include in DEPLOY_PLAYBOOK.read_text(encoding="utf-8")
    assert include in RECOVERY_PLAYBOOK.read_text(encoding="utf-8")


def _tasks() -> dict[str, dict]:
    def flatten(value: object, inherited_when: str | None = None) -> list[dict]:
        if isinstance(value, list):
            tasks: list[dict] = []
            for entry in value:
                tasks.extend(flatten(entry, inherited_when))
            return tasks
        if not isinstance(value, dict):
            return []
        own_when = value.get("when")
        if isinstance(own_when, str) and inherited_when:
            effective_when = f"({inherited_when}) and ({own_when})"
        elif isinstance(own_when, str):
            effective_when = own_when
        else:
            effective_when = inherited_when
        task = dict(value)
        if effective_when:
            task["when"] = effective_when
        tasks = [task] if isinstance(task.get("name"), str) else []
        for child in value.values():
            if isinstance(child, list | dict):
                tasks.extend(flatten(child, effective_when))
        return tasks

    tasks: dict[str, dict] = {}
    for path in (CORE_UNITS_TASKS, KOPIA_UNITS_TASKS):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        tasks.update({task["name"]: task for task in flatten(document)})
    return tasks


def test_kopia_snapshot_service_retries_transient_failures_with_a_bounded_rate() -> None:
    task = _tasks()["Install Kopia snapshot service"]
    unit = task["ansible.builtin.copy"]["content"]

    assert "Restart=on-failure" in unit
    assert "RestartSec=15min" in unit
    assert "StartLimitIntervalSec=1h" in unit
    assert "StartLimitBurst=3" in unit


def test_disabled_backups_stop_and_remove_snapshot_units() -> None:
    tasks = _tasks()
    stop = tasks["Stop disabled Kopia timers"]
    remove = tasks["Remove disabled Kopia units"]

    assert stop["when"] == "not (homelab_backups_enabled | default(false) | bool)"
    assert stop["ansible.builtin.systemd"]["state"] == "stopped"
    assert stop["ansible.builtin.systemd"]["enabled"] is False
    assert set(stop["loop"]) == {"kopia-snapshot.timer", "kopia-restore-drill.timer"}
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert set(remove["loop"]) == {
        "/etc/systemd/system/kopia-snapshot.service",
        "/etc/systemd/system/kopia-snapshot.timer",
        "/etc/systemd/system/kopia-restore-drill.service",
        "/etc/systemd/system/kopia-restore-drill.timer",
    }


def test_maintenance_timer_uses_generated_schedule_and_enable_flag() -> None:
    tasks = _tasks()
    install = tasks["Install homelab maintenance timer unit"]
    enable = tasks["Reconcile homelab maintenance timer"]

    assert "OnCalendar={{ homelab_maintenance_calendar }}" in install["ansible.builtin.copy"]["content"]
    assert "homelab_maintenance_enabled" in str(enable["ansible.builtin.systemd"]["enabled"])
    assert "homelab_maintenance_enabled" in enable["ansible.builtin.systemd"]["state"]


def test_watchdog_timer_is_explicitly_node_local() -> None:
    service = _tasks()["Install homelab watchdog timer"]["ansible.builtin.copy"]["content"]

    assert "Environment=HOMELAB_NODE={{ homelab_node_id }}" in service


def test_rightsize_timer_is_configurable_and_control_node_only() -> None:
    tasks = _tasks()
    service = tasks["Install verified resource tuning service"]
    timer = tasks["Install verified resource tuning timer"]
    reconcile = tasks["Reconcile verified resource tuning timer"]

    assert service["when"] == "homelab_node_id == control_node"
    assert "watchdog rightsize --apply" in service["ansible.builtin.copy"]["content"]
    assert timer["when"] == "homelab_node_id == control_node"
    assert "OnUnitActiveSec={{ homelab_rightsize_interval_hours }}h" in timer["ansible.builtin.copy"]["content"]
    assert "homelab_rightsize_enabled" in str(reconcile["ansible.builtin.systemd"])
    assert reconcile["when"] == "homelab_node_id == control_node"


def test_non_control_nodes_remove_resource_tuning_units() -> None:
    tasks = _tasks()
    disable = tasks["Disable resource tuning outside the control node"]
    remove = tasks["Remove resource tuning units outside the control node"]

    assert disable["when"] == "homelab_node_id != control_node"
    assert set(remove["loop"]) == {
        "/etc/systemd/system/homelab-rightsize.service",
        "/etc/systemd/system/homelab-rightsize.timer",
    }


def test_kopia_restore_drill_is_weekly_bounded_and_runs_on_its_service_node() -> None:
    tasks = _tasks()
    service = tasks["Install Kopia restore drill service on the repository node"]
    timer = tasks["Install Kopia restore drill timer on the repository node"]
    enable = tasks["Enable Kopia restore drill timer on the repository node"]

    service_unit = service["ansible.builtin.copy"]["content"]
    timer_unit = timer["ansible.builtin.copy"]["content"]
    assert "maintenance backup-drill" in service_unit
    assert "Restart=on-failure" in service_unit
    assert "StartLimitBurst=3" in service_unit
    assert "OnCalendar=Sun *-*-* 05:30:00" in timer_unit
    assert "Persistent=true" in timer_unit
    for task in (service, timer, enable):
        assert "homelab_backups_enabled | default(false)" in task["when"]
        assert "homelab_node_id == service_nodes['kopia']" in task["when"]


def test_restore_drill_units_are_removed_after_kopia_moves_nodes() -> None:
    tasks = _tasks()
    disable = tasks["Disable restore drill outside the Kopia repository node"]
    remove = tasks["Remove restore drill units outside the Kopia repository node"]

    assert "homelab_node_id != service_nodes['kopia']" in disable["when"]
    assert disable["ansible.builtin.systemd"]["state"] == "stopped"
    assert set(remove["loop"]) == {
        "/etc/systemd/system/kopia-restore-drill.service",
        "/etc/systemd/system/kopia-restore-drill.timer",
    }
