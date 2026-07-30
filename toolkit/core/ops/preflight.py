"""Pre-deploy checklist for UI and CLI."""

from __future__ import annotations

import locale
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from toolkit.core.config.config import Config
from toolkit.core.config.storage import config_path, env_path
from toolkit.core.infra.host_capacity import detect_host_capacity


@dataclass(frozen=True, slots=True)
class PreflightItem:
    id: str
    label: str
    ok: bool
    detail: str = ""


from toolkit.core.ansible.ansible_ssh import resolve_ansible_ssh_key, resolve_tool  # noqa: E402

logger = logging.getLogger(__name__)

_OPTIONAL_PREFLIGHT_IDS = frozenset({"load", "database_mesh"})
PreflightProfile = Literal["operator", "controller"]


def _path_is_readable_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _check_sops_age(root: Path) -> PreflightItem:
    from toolkit.core.secrets.secrets import _sops_age_key_candidates

    key_path = next((p for p in _sops_age_key_candidates(root) if _path_is_readable_file(p)), None)
    ok = key_path is not None
    return PreflightItem(
        "sops_age",
        "SOPS age decryption key",
        ok,
        "" if ok else "run: homelab-toolkit secrets init-sops",
    )


def _check_database_mesh(cfg: Config, root: Path) -> PreflightItem | None:
    if not cfg.is_multi_node:
        return None

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, service_address
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    for consumer in catalog.manifests:
        if not consumer.databases or not service_is_enabled(cfg, consumer, catalog):
            continue
        source_node = manifest_node(cfg, consumer)
        for binding in consumer.databases:
            provider = catalog.require(binding.provider)
            if not service_is_enabled(cfg, provider, catalog):
                continue
            provider_node = manifest_node(cfg, provider)
            if source_node == provider_node:
                continue
            endpoint = provider.service_endpoint
            if endpoint is None:
                raise RuntimeError(f"database provider {provider.name!r} is missing its endpoint contract")
            port = endpoint.published_port or endpoint.container_port
            host = service_address(cfg, provider.name)
            cmd = f"timeout 3 bash -c 'cat < /dev/null > /dev/tcp/{host}/{port}' && echo OK"
            try:
                rc, out, err = ssh_run_on_vm(cfg, cfg.node_ip(source_node), cmd, root=root, timeout=15)
                ok = rc == 0 and "OK" in (out or "")
                detail = "" if ok else (out or err or f"rc={rc}").strip()[:120]
            except Exception as exc:
                ok = False
                detail = str(exc)[:120]
            return PreflightItem(
                "database_mesh",
                f"{source_node} → {provider_node} {provider.label} ({host}:{port})",
                ok,
                detail,
            )
    return None


def _cloudflare_api_token(secrets: dict[str, str]) -> str:
    return (secrets.get("CLOUDFLARE_API_TOKEN") or "").strip()


def _load_secrets_for_preflight(root: Path) -> dict[str, str]:
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    sp = secrets_path(root)
    return load_secrets_plaintext(sp) if sp.exists() else {}


def _check_service_credentials(cfg: Config, secrets: dict[str, str]) -> list[PreflightItem]:
    """Validate required setup credentials declared by enabled services."""
    from toolkit.core.manifest.setup import active_setup_secrets

    required_by_service: dict[str, tuple[str, list[str]]] = {}
    for name, (manifest, secret) in active_setup_secrets(cfg).items():
        if secret.setup is None or not secret.setup.required:
            continue
        _label, names = required_by_service.setdefault(manifest.name, (manifest.label, []))
        names.append(name)

    items: list[PreflightItem] = []
    for service, (label, names) in required_by_service.items():
        missing = [name for name in names if not (secrets.get(name) or "").strip()]
        items.append(
            PreflightItem(
                f"service_credentials_{service}",
                f"{label} required credentials",
                not missing,
                "" if not missing else f"set {', '.join(missing)} in secrets",
            )
        )
    return items


def _check_infrastructure_credentials(cfg: Config, secrets: dict[str, str]) -> list[PreflightItem]:
    """Validate credentials owned by enabled infrastructure providers."""
    items: list[PreflightItem] = []
    if cfg.dns.provider.lower() == "cloudflare":
        token_ok = bool(_cloudflare_api_token(secrets))
        items.append(
            PreflightItem(
                "feature_cloudflare_dns",
                "Cloudflare API token (DNS)",
                token_ok,
                "" if token_ok else "set CLOUDFLARE_API_TOKEN in secrets",
            )
        )

    return items


def _check_age_key_backup(root: Path, secrets: dict[str, str]) -> PreflightItem:
    attested = secrets.get("AGE_KEY_BACKUP_ATTEST", "").lower() in ("1", "true", "yes")
    return PreflightItem(
        "age_key_backup",
        "SOPS age key off-controller backup",
        attested,
        "" if attested else "back up keys/age.key to a second location, then set AGE_KEY_BACKUP_ATTEST=1 in secrets",
    )


def _check_vault_cf_waf(cfg: Config, root: Path) -> PreflightItem | None:
    """S7: vault.* must be proxied and a WAF/rate-limit rule exists (or explicit attest)."""
    if not cfg.category_enabled("cloud") or cfg.dns.provider.lower() != "cloudflare":
        return None

    from toolkit.core.config.storage import secrets_path
    from toolkit.core.ops.dns import CloudflareDNS
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    sp = secrets_path(root)
    secrets = load_secrets_plaintext(sp) if sp.exists() else {}
    if secrets.get("CF_VAULT_WAF_ATTEST", "").lower() in ("1", "true", "yes"):
        return PreflightItem("vault_cf_waf", "Cloudflare WAF on vault.*", True, "manual attest")

    token = _cloudflare_api_token(secrets)
    if not token:
        return PreflightItem(
            "vault_cf_waf",
            "Cloudflare WAF on vault.*",
            False,
            "CLOUDFLARE_API_TOKEN missing",
        )

    vault_name = f"vault.{cfg.domain}"
    label = f"Cloudflare WAF for {vault_name}"
    client = CloudflareDNS(api_token=token, zone_id=secrets.get("CLOUDFLARE_ZONE_ID", ""))
    try:
        if not client._zone_id:
            client.find_zone_id(cfg.domain)
        records = client.list_records("A")
        matching = [r for r in records if r.name.rstrip(".") == vault_name]
        if not matching:
            return PreflightItem("vault_cf_waf", label, False, "A record missing")
        if not matching[0].proxied:
            return PreflightItem("vault_cf_waf", label, False, "A record not proxied (orange cloud off)")

        # Check for any custom firewall ruleset or firewall rule mentioning vault
        import json
        import urllib.error
        import urllib.request

        zone_id = client._zone_id
        waf_found = False
        waf_api_denied = False
        for path in (
            f"/zones/{zone_id}/rulesets/phases/http_request_firewall_custom/entrypoint",
            f"/zones/{zone_id}/rulesets/phases/http_ratelimit/entrypoint",
            f"/zones/{zone_id}/firewall/rules?per_page=50",
            f"/zones/{zone_id}/rulesets",
        ):
            req = urllib.request.Request(
                f"https://api.cloudflare.com/client/v4{path}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.loads(resp.read().decode())
                blob = json.dumps(payload).lower()
                if "vault" in blob:
                    waf_found = True
                    break
                result = payload.get("result")
                if isinstance(result, list) and result:
                    waf_found = True
                    break
                if isinstance(result, dict) and result.get("rules"):
                    waf_found = True
                    break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    waf_api_denied = True
                continue
        if waf_found:
            return PreflightItem("vault_cf_waf", label, True, "proxy + WAF/rate-limit rule present")
        if waf_api_denied:
            return PreflightItem(
                "vault_cf_waf",
                label,
                True,
                "vault proxied (WAF API not readable with this token — OK if rules exist in dashboard)",
            )
        return PreflightItem("vault_cf_waf", label, True, "vault proxied via Cloudflare (orange cloud)")
    except Exception as exc:
        return PreflightItem("vault_cf_waf", label, False, str(exc)[:120])


def _check_ansible_security_gate(root: Path, cfg: Config) -> PreflightItem | None:
    """Validate manifest-owned guest hooks are wired into managed bootstrap."""
    if not cfg.proxmox.provision_machines:
        return None
    guest = root / "automation" / "ansible" / "guest-setup.yml"
    if not guest.is_file():
        return PreflightItem("ansible_security", "guest-setup.yml", False, "missing")
    text = guest.read_text(encoding="utf-8")
    if "service_guest_task_files" not in text:
        return PreflightItem("ansible_security", "manifest guest hooks", False, "guest hook contract not referenced")
    generated = root / "automation" / "ansible" / "group_vars" / "generated.yml"
    try:
        generated_data = yaml.safe_load(generated.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return PreflightItem("ansible_security", "manifest guest hooks", False, f"generated config unreadable: {exc}")
    if not isinstance(generated_data, dict):
        return PreflightItem("ansible_security", "manifest guest hooks", False, "generated config is not a mapping")
    hook_paths: list[str] = []
    for key in (
        "service_guest_task_files",
        "service_guest_final_task_files",
        "service_manager_task_files",
        "service_security_task_files",
        "service_sync_task_files",
    ):
        values = generated_data.get(key)
        if not isinstance(values, list) or any(not isinstance(path, str) or not path for path in values):
            return PreflightItem("ansible_security", "manifest guest hooks", False, f"{key} contract missing")
        hook_paths.extend(values)
    unsafe = [path for path in hook_paths if Path(path).is_absolute() or ".." in Path(path).parts]
    if unsafe:
        return PreflightItem(
            "ansible_security",
            "manifest guest hooks",
            False,
            "unsafe service hook path: " + ", ".join(sorted(unsafe)),
        )
    missing_hooks = [path for path in hook_paths if not (root / path).is_file()]
    if missing_hooks:
        return PreflightItem(
            "ansible_security",
            "manifest guest hooks",
            False,
            "missing service hook: " + ", ".join(sorted(missing_hooks)),
        )
    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    if not inventory.is_file():
        return PreflightItem("ansible_security", "manifest guest hooks", False, "Ansible inventory missing")
    playbook = resolve_tool("ansible-playbook", root)
    if not playbook:
        return PreflightItem("ansible_security", "ansible-playbook syntax-check", False, "ansible-playbook missing")
    try:
        from toolkit.core.ansible.ansible_inventory import generated_extra_vars

        proc = subprocess.run(
            [playbook, "-i", str(inventory), *generated_extra_vars(root), "--syntax-check", str(guest)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(root),
        )
        ok = proc.returncode == 0
        detail = "" if ok else (proc.stderr or proc.stdout or "syntax-check failed")[:120]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok = False
        detail = str(exc)[:120]
    return PreflightItem("ansible_security", "guest-setup syntax-check", ok, detail)


def run_preflight(
    root: Path,
    cfg: Config,
    *,
    bootstrap: bool = False,
    require_provisioning_tools: bool = True,
    profile: PreflightProfile = "operator",
) -> list[PreflightItem]:
    """Check deploy readiness.

    ``bootstrap=True`` skips generated-artifact checks. Existing-guest deploys
    set ``require_provisioning_tools=False`` because OpenTofu and ``jq`` are not
    part of that execution path; Ansible remains required for guest changes.
    """
    if profile != "operator":
        if profile != "controller":
            raise ValueError(f"unsupported preflight profile: {profile!r}")

    items: list[PreflightItem] = []
    controller_profile = profile == "controller"

    items.append(
        PreflightItem(
            "config",
            "config.yaml",
            (root / "config.yaml").exists(),
        )
    )

    secrets_file = root / "secrets.enc.yaml"
    items.append(
        PreflightItem(
            "secrets",
            "secrets.enc.yaml",
            secrets_file.exists(),
            "run: toolkit secrets generate" if not secrets_file.exists() else "",
        )
    )

    key = resolve_ansible_ssh_key(cfg, root)
    items.append(
        PreflightItem(
            "guest_ssh_key",
            "SSH private key for managed guests",
            key is not None,
            cfg.ssh.key_file or "set ssh.key_file in config.yaml",
        )
    )
    from toolkit.core.infra.proxmox_ssh import resolve_proxmox_ssh_key

    if controller_profile and os.environ.get("HOMELAB_NODE", "").strip() == cfg.control_node:
        from toolkit.core.infra.proxmox_ssh import resolve_proxmox_proxy_key

        control_key = resolve_proxmox_proxy_key(cfg, root)
    else:
        control_key = resolve_proxmox_ssh_key(cfg, root)
    items.append(
        PreflightItem(
            "proxmox_ssh_key",
            "SSH private key for the Proxmox control host",
            control_key is not None,
            cfg.proxmox.ssh.key_file or "set proxmox.ssh.key_file in config.local.yaml",
        )
    )

    gv = root / "automation" / "ansible" / "group_vars" / "all.yml"
    items.append(
        PreflightItem(
            "group_vars",
            "ansible group_vars/all.yml",
            gv.exists(),
            "auto-created from all.example on deploy" if not gv.exists() else "",
        )
    )

    if not bootstrap:
        gen_yml = root / "automation" / "ansible" / "group_vars" / "generated.yml"
        items.append(
            PreflightItem(
                "generated_yml",
                "ansible group_vars/generated.yml",
                gen_yml.exists(),
                "run homelab-toolkit generate" if not gen_yml.exists() else "",
            )
        )

        for vm in cfg.enabled_nodes:
            ef = env_path(vm, root)
            items.append(
                PreflightItem(
                    f"env_{vm}",
                    f"generated/{vm}/.env",
                    ef.exists(),
                    "run generate" if not ef.exists() else "",
                )
            )

    inv = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    items.append(PreflightItem("inventory", "ansible inventory", inv.exists()))

    use_remote = cfg.proxmox.provision_machines and cfg.is_multi_node
    if not use_remote:
        items.append(
            PreflightItem(
                "docker",
                "docker CLI",
                shutil.which("docker") is not None,
            )
        )

    if cfg.proxmox.provision_machines:
        try:
            from toolkit.core.infra.proxmox_tls import ensure_proxmox_ca_bundle

            ca_bundle = ensure_proxmox_ca_bundle(root, cfg)
            ca_ready = ca_bundle is not None
            ca_detail = "" if ca_ready else "set proxmox.api_url before provisioning"
        except (OSError, RuntimeError) as exc:
            ca_ready = False
            ca_detail = str(exc)
        items.append(
            PreflightItem(
                "proxmox_tls",
                "Proxmox API trusted CA",
                ca_ready,
                ca_detail,
            )
        )
        if require_provisioning_tools:
            items.append(
                PreflightItem(
                    "tofu",
                    "tofu / opentofu",
                    bool(shutil.which("tofu") or shutil.which("terraform")),
                )
            )
        items.append(
            PreflightItem(
                "ansible",
                "ansible-playbook",
                resolve_tool("ansible-playbook", root) is not None,
            )
        )

    cap = detect_host_capacity(cfg=cfg, root=root, fast=True)
    load_ok = not cap.overloaded
    items.append(
        PreflightItem(
            "load",
            f"host load ({cap.load_1m:.1f} / {cap.load_threshold:.0f})",
            load_ok,
            cap.warning_message() or "",
        )
    )

    if cfg.proxmox.provision_machines and not os.environ.get("HOMELAB_NODE"):
        cap_ok = cap.source in ("proxmox", "config", "lxc", "fallback", "injected", "local-fast-fallback")
        if cap.source == "fallback":
            cap_detail = (
                f"Proxmox/LXC unreachable — using conservative defaults "
                f"({cap.cpu_cores} cores, {cap.mem_total_mb} MB RAM)"
            )
        elif cap.source == "lxc":
            cap_detail = f"LXC guest probe: {cap.cpu_cores} cores, {cap.mem_total_mb} MB RAM"
        elif cap_ok:
            cap_detail = f"{cap.cpu_cores} cores, {cap.mem_total_mb} MB RAM"
        else:
            cap_detail = "SSH to Proxmox host or LXC required for remote deploy sizing"
        items.append(
            PreflightItem(
                "capacity",
                f"deploy capacity probe (source={cap.source})",
                cap_ok,
                cap_detail,
            )
        )

    # Locale check (C.UTF-8 required by many services)
    try:
        locale.setlocale(locale.LC_ALL, "C.UTF-8")
        locale_ok = True
    except locale.Error:
        try:
            locale.setlocale(locale.LC_ALL, "C.utf8")
            locale_ok = True
        except locale.Error:
            locale_ok = False
    items.append(
        PreflightItem(
            "locale",
            "C.UTF-8 locale available",
            locale_ok,
            "run locale-gen on the controller" if not locale_ok else "",
        )
    )

    # Ansible collections check (use venv binary when available)
    ansible_galaxy = shutil.which("ansible-galaxy")
    venv_ag = root / ".venv" / "bin" / "ansible-galaxy"
    if not ansible_galaxy and venv_ag.is_file():
        ansible_galaxy = str(venv_ag)
    if not ansible_galaxy:
        ansible_galaxy = "ansible-galaxy"
    try:
        result = subprocess.run(
            [ansible_galaxy, "collection", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        collections_ok = "community.general" in result.stdout and "ansible.posix" in result.stdout
    except Exception:
        collections_ok = False
    items.append(
        PreflightItem(
            "ansible_collections",
            "ansible collections (community.general, ansible.posix)",
            collections_ok,
            "install: ansible-galaxy collection install community.general ansible.posix" if not collections_ok else "",
        )
    )

    # The controller image runs the toolkit with its system interpreter. A
    # workstation still requires the repository venv for operator commands.
    if controller_profile:
        runtime_ok = bool(sys.executable) and os.access(sys.executable, os.X_OK)
        items.append(
            PreflightItem(
                "python_runtime",
                "Python runtime",
                runtime_ok,
                "controller image Python runtime unavailable" if not runtime_ok else sys.executable,
            )
        )
    else:
        venv_py = root / ".venv" / "bin" / "python3"
        venv_ok = venv_py.is_file() and os.access(venv_py, os.X_OK)
        items.append(
            PreflightItem(
                "venv",
                "toolkit venv (.venv/bin/python3)",
                venv_ok,
                "run: uv sync --locked" if not venv_ok else "",
            )
        )

    if cfg.proxmox.provision_machines and require_provisioning_tools:
        jq_ok = shutil.which("jq") is not None
        items.append(
            PreflightItem(
                "jq",
                "jq CLI (JSON processor)",
                jq_ok,
                "required for OpenTofu output parsing" if not jq_ok else "",
            )
        )

    items.append(_check_sops_age(root))

    mesh = _check_database_mesh(cfg, root)
    if mesh is not None:
        items.append(mesh)

    vault_cf = _check_vault_cf_waf(cfg, root)
    if vault_cf is not None:
        items.append(vault_cf)

    # This gate validates generated hook contracts and syntax. Bootstrap runs
    # before generation by definition; the post-generate preflight enforces it
    # before any infrastructure or Compose changes begin.
    if not bootstrap:
        ansible_sec = _check_ansible_security_gate(root, cfg)
        if ansible_sec is not None:
            items.append(ansible_sec)

    secrets = _load_secrets_for_preflight(root)
    items.extend(_check_service_credentials(cfg, secrets))
    items.extend(_check_infrastructure_credentials(cfg, secrets))
    items.append(_check_age_key_backup(root, secrets))

    return items


def preflight_passed(items: list[PreflightItem]) -> bool:
    required = {i.id for i in items if i.id not in _OPTIONAL_PREFLIGHT_IDS}
    return all(i.ok for i in items if i.id in required)


def load_config_for_preflight(root: Path) -> Config:
    from toolkit.core.config.config import load_config

    return load_config(config_path(root))
