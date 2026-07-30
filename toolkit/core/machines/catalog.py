"""Discover strict packaged and project-owned machine templates."""

from __future__ import annotations

from pathlib import Path

import yaml

from toolkit.core.machines.models import MachineSpec, validate_machine_id


def _package_catalog_root() -> Path:
    return Path(__file__).resolve().parents[2] / "machines"


def _load_catalog(catalog_root: Path) -> dict[str, MachineSpec]:
    machines: dict[str, MachineSpec] = {}
    for path in sorted(catalog_root.glob("*/machine.yaml")):
        machine_id = validate_machine_id(path.parent.name)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path} must contain a mapping")
        machines[machine_id] = MachineSpec.model_validate(document)
    return machines


def load_default_machines() -> dict[str, MachineSpec]:
    """Load the shipped default topology when config declares no instances."""
    machines = _load_catalog(_package_catalog_root())
    if not machines:
        raise ValueError("default machine catalog is empty")
    return machines


def load_machine_templates(root: Path | None = None) -> dict[str, MachineSpec]:
    """Load packaged templates plus project-owned ``machines/*/machine.yaml`` files."""
    templates = load_default_machines()
    if root is None:
        return templates
    project_templates = _load_catalog(root.resolve() / "machines")
    duplicates = sorted(set(templates) & set(project_templates))
    if duplicates:
        raise ValueError(f"project machine templates duplicate packaged IDs: {', '.join(duplicates)}")
    return {**templates, **project_templates}
