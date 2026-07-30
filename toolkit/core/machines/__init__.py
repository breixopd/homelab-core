"""Machine desired-state models and template discovery."""

from toolkit.core.machines.catalog import load_default_machines, load_machine_templates
from toolkit.core.machines.models import MachineDisk, MachineSpec

__all__ = ["MachineDisk", "MachineSpec", "load_default_machines", "load_machine_templates"]
