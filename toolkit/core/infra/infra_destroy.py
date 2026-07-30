"""Destroy managed Proxmox guests and clean OpenTofu state after verification."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.deploy.operation_lease import OperationLease
    from toolkit.core.machines import MachineSpec

_TFSTATE_PATTERNS = ("terraform.tfstate", "terraform.tfstate.backup")
_TFSTATE_DIRS = (".terraform", ".tofu")


def _inventory_matches_machine(item: object, machine: MachineSpec) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("name") == machine.hostname:
        return True
    try:
        return int(item.get("vmid", -1)) == machine.vmid
    except (TypeError, ValueError):
        return False


def verify_proxmox_absence(root: Path) -> None:
    """Fail unless the Proxmox API independently proves managed guests are gone."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.deploy.destructive_guard import ResourcesStillPresentError
    from toolkit.core.infra.proxmox import list_proxmox_lxcs, list_proxmox_vms
    from toolkit.core.infra.proxmox_tls import ensure_proxmox_ca_bundle
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    token_id = secrets.get("PROXMOX_API_TOKEN_ID", "").strip()
    token_secret = secrets.get("PROXMOX_API_TOKEN_SECRET", "").strip()
    if not token_id:
        raise RuntimeError("Proxmox API token is unavailable for post-destroy verification")
    ca_bundle = ensure_proxmox_ca_bundle(root, cfg)
    if ca_bundle is None:
        raise RuntimeError("Proxmox CA trust is unavailable for post-destroy verification")
    inventory_args = (
        cfg.proxmox.api_url,
        token_id,
        token_secret,
        cfg.proxmox.node,
    )
    inventory = [
        *list_proxmox_lxcs(*inventory_args, verify_ssl=True, ca_file=str(ca_bundle)),
        *list_proxmox_vms(*inventory_args, verify_ssl=True, ca_file=str(ca_bundle)),
    ]
    remaining = [
        f"{machine_id} ({machine.hostname}, VMID {machine.vmid})"
        for machine_id, machine in cfg.machines.items()
        if machine.enabled and machine.managed and any(_inventory_matches_machine(item, machine) for item in inventory)
    ]
    if remaining:
        raise ResourcesStillPresentError("Target managed guests still exist after destroy: " + ", ".join(remaining))


def verify_proxmox_machine_absence(root: Path, machine_id: str) -> None:
    """Fail unless Proxmox independently proves one configured guest is absent."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.infra.proxmox import list_proxmox_lxcs, list_proxmox_vms
    from toolkit.core.infra.proxmox_tls import ensure_proxmox_ca_bundle
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    cfg = load_config(config_path(root))
    try:
        machine = cfg.machines[machine_id]
    except KeyError as exc:
        raise RuntimeError(f"Machine {machine_id!r} is not configured") from exc
    secrets = load_secrets_plaintext(secrets_path(root))
    token_id = secrets.get("PROXMOX_API_TOKEN_ID", "").strip()
    token_secret = secrets.get("PROXMOX_API_TOKEN_SECRET", "").strip()
    if not token_id:
        raise RuntimeError("Proxmox API token is unavailable for post-retirement verification")
    ca_bundle = ensure_proxmox_ca_bundle(root, cfg)
    if ca_bundle is None:
        raise RuntimeError("Proxmox CA trust is unavailable for post-retirement verification")
    inventory_loader = list_proxmox_lxcs if machine.kind == "lxc" else list_proxmox_vms
    inventory = inventory_loader(
        cfg.proxmox.api_url,
        token_id,
        token_secret,
        cfg.proxmox.node,
        verify_ssl=True,
        ca_file=str(ca_bundle),
    )

    remaining = [item for item in inventory if _inventory_matches_machine(item, machine)]
    if remaining:
        raise RuntimeError(f"Proxmox still reports machine {machine_id!r} after retirement")


def _machine_tofu_targets(machine_id: str, kind: str) -> tuple[str, ...]:
    resource = (
        "proxmox_virtual_environment_container.machine" if kind == "lxc" else "proxmox_virtual_environment_vm.machine"
    )
    targets = [f'{resource}["{machine_id}"]', f'random_password.machine_root["{machine_id}"]']
    if kind == "vm":
        targets.append(f'proxmox_download_file.vm_image["{machine_id}"]')
    return tuple(targets)


def retire_machine_infrastructure(
    root: Path,
    machine_id: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> int:
    """Destroy one managed guest without changing any other OpenTofu resource."""
    log = on_log or (lambda _message: None)
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    cfg = load_config(config_path(root))
    machine = cfg.machines.get(machine_id)
    if machine is None or not machine.enabled or not machine.managed:
        log(f"✗ Machine {machine_id!r} is not an enabled managed resource")
        return 1
    infra_dir = root / "infrastructure"
    if not infra_dir.is_dir():
        log(f"✗ Infrastructure directory not found: {infra_dir}")
        return 1
    try:
        from toolkit.core.infra.iac_sync import sync_from_repo_root

        sync_from_repo_root(root)
    except (OSError, ValueError) as exc:
        log(f"✗ Could not synchronize OpenTofu desired state: {exc}")
        return 1
    tofu = shutil.which("tofu") or shutil.which("terraform")
    if not tofu:
        log("✗ OpenTofu (tofu) not found on PATH")
        return 1
    from toolkit.core.infra.infra_env import load_tofu_env

    env = load_tofu_env(root, allow_destroy=True)
    if not (infra_dir / ".terraform").exists() and not (infra_dir / ".tofu").exists():
        log("--> Initializing OpenTofu...")
        initialized = subprocess.run(
            [tofu, "init", "-input=false"],
            cwd=str(infra_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if initialized.stdout:
            log(initialized.stdout.rstrip())
        if initialized.returncode != 0:
            log(initialized.stderr or "tofu init failed")
            return initialized.returncode or 1
    targets = _machine_tofu_targets(machine_id, machine.kind)
    command = [tofu, "destroy", "-input=false", "-auto-approve", *(f"-target={target}" for target in targets)]
    log(f"--> Retiring managed machine {machine_id} (VMID {machine.vmid})...")
    destroyed = subprocess.run(
        command,
        cwd=str(infra_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    for line in (destroyed.stdout or "").splitlines():
        log(line)
    for line in (destroyed.stderr or "").splitlines():
        log(line)
    if destroyed.returncode != 0:
        return destroyed.returncode
    state = subprocess.run(
        [tofu, "state", "list"],
        cwd=str(infra_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if state.returncode != 0:
        log(state.stderr or "Could not verify OpenTofu state after retirement")
        return state.returncode or 1
    remaining = sorted(set(state.stdout.splitlines()) & set(targets))
    if remaining:
        log(f"✗ OpenTofu still tracks retired resources: {', '.join(remaining)}")
        return 1
    try:
        verify_proxmox_machine_absence(root, machine_id)
    except Exception as exc:
        log(f"✗ Independent Proxmox retirement verification failed: {exc}")
        return 1
    return 0


def retire_machine_infrastructure_guarded(
    root: Path,
    machine_id: str,
    *,
    on_log: Callable[[str], None] | None = None,
    checkpoint_max_age: timedelta = timedelta(days=7),
    operation_lease: OperationLease | None = None,
) -> int:
    """Retire one machine while holding the destructive lease and recovery proof."""
    from toolkit.core.deploy.destructive_guard import require_verified_checkpoint
    from toolkit.core.deploy.operation_lease import OperationLease

    lease = operation_lease or OperationLease.acquire(root, "retire-machine")
    owns_lease = operation_lease is None
    try:
        lease.assert_owns_root(root)
        lease.raise_if_cancelled()
        require_verified_checkpoint(root, [machine_id], checkpoint_max_age)
        return retire_machine_infrastructure(root, machine_id, on_log=on_log)
    finally:
        if owns_lease:
            lease.release()


def clean_tofu_state(root: Path, *, on_log: Callable[[str], None] | None = None) -> int:
    """Remove tofu state files + plugin cache for a from-scratch redeploy.

    Consolidates the 3 previously-duplicated cleanup sites
    (infra_destroy, deploy_workflow:1722, deploy_cmd:126) into one path so the
    pattern set + dirs can't drift. Returns the count of files/dirs removed.
    """
    log = on_log or (lambda _msg: None)
    infra_dir = root / "infrastructure"
    removed = 0
    for pattern in _TFSTATE_PATTERNS:
        f = infra_dir / pattern
        if f.exists():
            try:
                f.unlink()
                removed += 1
            except OSError as exc:
                log(f"⚠ Could not remove {f}: {exc}")
    for d in _TFSTATE_DIRS:
        target = infra_dir / d
        if target.exists():
            try:
                shutil.rmtree(target)
                removed += 1
            except OSError as exc:
                log(f"⚠ Could not remove {target}: {exc}")
    return removed


def destroy_infrastructure(
    root: Path,
    *,
    on_log: Callable[[str], None] | None = None,
    auto_approve: bool = True,
) -> int:
    """Run tofu destroy in infrastructure/. Returns process exit code."""
    log = on_log or (lambda _msg: None)
    infra_dir = root / "infrastructure"
    if not infra_dir.is_dir():
        log(f"✗ Infrastructure directory not found: {infra_dir}")
        return 1

    # Ensure generated.auto.tfvars is current before tofu runs.
    try:
        from toolkit.core.infra.iac_sync import sync_from_repo_root

        sync_from_repo_root(root)
    except OSError:
        pass

    tofu = shutil.which("tofu") or shutil.which("terraform")
    if not tofu:
        log("✗ OpenTofu (tofu) not found on PATH")
        return 1

    from toolkit.core.infra.infra_env import load_tofu_env

    env = load_tofu_env(root, allow_destroy=True)  # destroy is the explicit operator intent

    if not (infra_dir / ".terraform").exists() and not (infra_dir / ".tofu").exists():
        log("--> Running tofu init...")
        init = subprocess.run(
            [tofu, "init", "-input=false"],
            cwd=str(infra_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        if init.stdout:
            log(init.stdout.rstrip())
        if init.returncode != 0:
            log(init.stderr or "tofu init failed")
            return init.returncode

    cmd = [tofu, "destroy", "-input=false"]
    if auto_approve:
        cmd.append("-auto-approve")

    log("--> Destroying managed infrastructure guests...")
    proc = subprocess.run(cmd, cwd=str(infra_dir), env=env, capture_output=True, text=True, timeout=600)
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(line)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(line)
    if proc.returncode != 0:
        return proc.returncode

    show = subprocess.run(
        [tofu, "show", "-json"], cwd=str(infra_dir), env=env, capture_output=True, text=True, timeout=60
    )
    if show.returncode != 0:
        log("✗ Could not verify post-destroy OpenTofu state")
        return show.returncode or 1
    try:
        state = json.loads(show.stdout or "{}")
        root_module = state.get("values", {}).get("root_module", {})
        resources = root_module.get("resources", [])
        remaining_guest_resources = [
            str(resource.get("address") or resource.get("type"))
            for resource in resources
            if resource.get("type") in {"proxmox_virtual_environment_container", "proxmox_virtual_environment_vm"}
        ]
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log(f"✗ Could not parse post-destroy OpenTofu state: {exc}")
        return 1

    if remaining_guest_resources:
        log("✗ OpenTofu still tracks managed guest resources: " + ", ".join(remaining_guest_resources))
        return 1

    try:
        verify_proxmox_absence(root)
    except Exception as exc:
        log(f"✗ Independent Proxmox inventory verification failed: {exc}")
        return 1

    try:
        from toolkit.core.ops.dns import cleanup_stale_homelab_dns

        deleted = cleanup_stale_homelab_dns(root, on_log=log)
        if deleted:
            log(f"--> Removed {deleted} stale Cloudflare DNS record(s)")
    except Exception as exc:
        log(f"⚠ DNS cleanup skipped: {exc}")

    return 0


def destroy_infrastructure_guarded(
    root: Path,
    *,
    on_log: Callable[[str], None] | None = None,
    scope: Iterable[str] | None = None,
    checkpoint_max_age: timedelta = timedelta(days=7),
    operation_lease: OperationLease | None = None,
) -> int:
    """Destroy infrastructure only while holding a lease and recovery proof."""
    from toolkit.core.deploy.destructive_guard import require_verified_checkpoint
    from toolkit.core.deploy.operation_lease import OperationLease

    lease = operation_lease or OperationLease.acquire(root, "destroy-infrastructure")
    owns_lease = operation_lease is None
    try:
        lease.assert_owns_root(root)
        lease.raise_if_cancelled()
        if scope is None:
            from toolkit.core.config.config import load_config
            from toolkit.core.config.storage import config_path

            scope = load_config(config_path(root)).enabled_nodes
        require_verified_checkpoint(root, scope, checkpoint_max_age)
        return destroy_infrastructure(root, on_log=on_log, auto_approve=True)
    finally:
        if owns_lease:
            lease.release()


def destroy_host_guarded(
    root: Path,
    *,
    on_log: Callable[[str], None] | None = None,
    checkpoint_max_age: timedelta = timedelta(days=7),
) -> int:
    """Destroy managed guests and ZFS, then remove local state in proven order."""
    from toolkit.core.ansible.ansible_inventory import generated_extra_vars
    from toolkit.core.ansible.ansible_ssh import resolve_tool
    from toolkit.core.deploy.destructive_guard import require_verified_checkpoint
    from toolkit.core.deploy.operation_lease import OperationLease

    log = on_log or (lambda _msg: None)
    root = root.resolve()
    lease = OperationLease.acquire(root, "destroy-host")
    try:
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path

        require_verified_checkpoint(root, load_config(config_path(root)).enabled_nodes, checkpoint_max_age)
        log("Destroying managed infrastructure guests...")
        code = destroy_infrastructure(root, on_log=log, auto_approve=True)
        if code != 0:
            log("Guest destruction or independent inventory verification failed; preserving all local state")
            return code

        ansible_dir = root / "automation" / "ansible"
        inventory = ansible_dir / "inventory" / "hosts.yml"
        if not inventory.is_file():
            log(f"Inventory not found: {inventory}; preserving local state")
            return 1

        log("Destroying ZFS pool on the Proxmox host...")
        ansible_playbook = resolve_tool("ansible-playbook", root) or "ansible-playbook"
        result = subprocess.run(
            [
                ansible_playbook,
                "-i",
                str(inventory),
                *generated_extra_vars(root),
                "-e",
                "zfs_wipe_enabled=true",
                str(ansible_dir / "host-setup.yml"),
                "--tags",
                "zfs-wipe",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            log(f"ZFS wipe failed: {result.stderr[:300]}")
            log("Preserving OpenTofu state and generated artifacts for recovery")
            return result.returncode or 1

        removed = clean_tofu_state(root, on_log=log)
        log(f"OpenTofu state cleaned ({removed} file/dir removed)")
        generated_dir = root / "generated"
        if generated_dir.exists():
            for item in generated_dir.iterdir():
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
        generated_dir.mkdir(parents=True, exist_ok=True)
        log("Generated artifacts cleaned")
        return 0
    finally:
        lease.release()
