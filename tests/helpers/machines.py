"""Topology builders for tests that need an explicit machine layout."""

from __future__ import annotations

import ipaddress

from toolkit.core.machines.catalog import load_default_machines
from toolkit.core.machines.models import MachineSpec


def single_control_machines(control: str = "infra") -> dict[str, MachineSpec]:
    machines = load_default_machines()
    if control not in machines:
        raise KeyError(control)
    return {
        machine_id: machine.model_copy(update={"enabled": machine_id == control})
        for machine_id, machine in machines.items()
    }


def enabled_machines(*enabled_ids: str) -> dict[str, MachineSpec]:
    machines = load_default_machines()
    unknown = sorted(set(enabled_ids) - set(machines))
    if unknown:
        raise KeyError(", ".join(unknown))
    enabled = set(enabled_ids)
    return {
        machine_id: machine.model_copy(update={"enabled": machine_id in enabled})
        for machine_id, machine in machines.items()
    }


def machines_with_addresses(**addresses: str) -> dict[str, MachineSpec]:
    machines = load_default_machines()
    unknown = sorted(set(addresses) - set(machines))
    if unknown:
        raise KeyError(", ".join(unknown))
    configured: dict[str, MachineSpec] = {}
    for machine_id, machine in machines.items():
        address = addresses.get(machine_id, machine.address)
        network = ipaddress.ip_network(f"{address}/{machine.cidr}", strict=False)
        gateway = network.network_address + 1
        if str(gateway) == address:
            gateway += 1
        configured[machine_id] = machine.model_copy(update={"address": address, "gateway": str(gateway)})
    return configured


def renamed_default_machines() -> dict[str, MachineSpec]:
    """Default capabilities and resources with no default machine IDs."""
    defaults = load_default_machines()
    return {
        "core": defaults["infra"].model_copy(update={"hostname": "core-01"}),
        "stream": defaults["media"].model_copy(update={"hostname": "stream-01"}),
        "data": defaults["apps"].model_copy(update={"hostname": "data-01"}),
    }
