from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from toolkit.core.compose.docker import profiles_for_categories
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path, env_path


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _compose_profiles_for_vm(cfg: Config, vm: str) -> str:
    load_all()
    cats = [cat for cat in enabled_categories(cfg) if cat.runtime_node(cfg) == vm]
    profiles = set(profiles_for_categories(cats, cfg))
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import runtime_profiles_for_node

    profiles.update(runtime_profiles_for_node(cfg, load_service_catalog(), vm))
    from toolkit.core.projects.compose import project_profiles_for_vm

    profiles.update(project_profiles_for_vm(cfg, vm))
    return ",".join(sorted(profiles))


def _run_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _validate_static_generated_files(root: Path, cfg: Config, report: ValidationReport) -> None:
    generated = root / "generated"
    if not generated.exists():
        report.errors.append("generated/ directory is missing")
        return

    caddyfile = generated / "Caddyfile"
    if not caddyfile.exists() or not caddyfile.read_text().strip():
        report.errors.append("generated/Caddyfile is missing or empty")
    else:
        report.checks.append("Caddyfile present")

    prom = generated / "prometheus.yml"
    if prom.exists():
        try:
            yaml.safe_load(prom.read_text())
            report.checks.append("Prometheus config parses as YAML")
        except yaml.YAMLError as exc:
            report.errors.append(f"generated/prometheus.yml is invalid YAML: {exc}")

    authelia = generated / "authelia.yml"
    if cfg.category_enabled("management"):
        if not authelia.exists():
            report.errors.append("generated/authelia.yml is missing")
        else:
            try:
                yaml.safe_load(authelia.read_text())
                report.checks.append("Authelia config parses as YAML")
            except yaml.YAMLError as exc:
                report.errors.append(f"generated/authelia.yml is invalid YAML: {exc}")


def _validate_cross_vm_ingress(root: Path, cfg: Config, report: ValidationReport) -> None:
    if not cfg.is_multi_node:
        return
    compose_path = root / "docker-compose.yml"
    if compose_path.is_file():
        try:
            document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            report.errors.append(f"cross-VM ingress could not parse docker-compose.yml: {exc}")
            return
    else:
        try:
            from toolkit.core.generate.compose_assemble import assemble_compose_text

            document = yaml.safe_load(assemble_compose_text(root, cfg, include_release=False)) or {}
        except (OSError, ValueError, yaml.YAMLError) as exc:
            report.errors.append(f"cross-VM ingress could not assemble the deployment model: {exc}")
            return
    services = document.get("services")
    if not isinstance(services, dict):
        report.errors.append("cross-VM ingress requires a Compose services map")
        return

    from toolkit.core.compose.ports import compose_published_ports
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_node
    from toolkit.core.manifest.routes import compile_routes
    from toolkit.services.sdk.caddy import caddy_cross_vm_upstream

    errors_before = len(report.errors)
    ingress = load_service_catalog(root).require_provider("ingress")
    checked: set[tuple[str, str, int, int]] = set()
    for route in compile_routes(cfg):
        if route.node == service_node(cfg, ingress.name) or route.file_server_root:
            continue
        upstream_service, separator, upstream_port_text = route.upstream.rpartition(":")
        if not separator:
            report.errors.append(f"cross-VM route {route.host} has an invalid upstream")
            continue
        try:
            upstream_port = int(upstream_port_text)
            target = caddy_cross_vm_upstream(
                cfg,
                route.node,
                upstream_port_text,
                published_port=route.published_port,
            )
            published_port = int(target.rsplit(":", 1)[1])
        except ValueError:
            report.errors.append(f"cross-VM route {route.host} has an invalid port")
            continue
        key = (route.host, upstream_service, upstream_port, published_port)
        if key in checked:
            continue
        checked.add(key)

        candidate_names = tuple(dict.fromkeys((upstream_service, route.compose_service)))
        reachable = False
        for candidate in candidate_names:
            service = services.get(candidate)
            if not isinstance(service, dict):
                continue
            for port in compose_published_ports(service):
                if (
                    port.target == upstream_port
                    and port.published == published_port
                    and port.host_ip not in {"127.0.0.1", "::1", "localhost"}
                ):
                    reachable = True
                    break
            if reachable:
                break
        if not reachable:
            report.errors.append(
                f"cross-VM route {route.host} requires {route.node} port "
                f"{published_port}->{upstream_port} on {upstream_service}"
            )

    if len(report.errors) == errors_before:
        report.checks.append("cross-VM ingress routes are reachable")


def _validate_env_files(root: Path, cfg: Config, report: ValidationReport) -> None:
    for vm in cfg.enabled_nodes:
        path = env_path(vm, root)
        if not path.exists():
            report.errors.append(f"generated/{vm}/.env is missing")
            continue
        if not path.read_text().strip():
            report.errors.append(f"generated/{vm}/.env is empty")
            continue
        report.checks.append(f"{vm} env file present")


def _validate_compose(root: Path, cfg: Config, report: ValidationReport) -> None:
    from toolkit.core.compose.docker import deployment_compose_path

    expected_models = [deployment_compose_path(cfg, root, vm) for vm in cfg.enabled_nodes]
    if not any(path.is_file() for path in expected_models):
        report.skipped.append("docker compose validation skipped: no deployment models generated")
        return
    if not shutil.which("docker"):
        report.skipped.append("docker compose validation skipped: docker not installed")
        return
    if os.environ.get("HOMELAB_DEPLOY_CONTROLLER", "").strip().lower() in ("1", "true", "yes"):
        report.skipped.append("docker compose validation skipped: deploy controller (validated on guests)")
        return

    for vm in cfg.enabled_nodes:
        compose_file = deployment_compose_path(cfg, root, vm)
        if not compose_file.is_file():
            report.errors.append(f"Compose model is missing for {vm}: {compose_file.relative_to(root)}")
            continue
        vm_env = env_path(vm, root)
        if not vm_env.exists():
            continue
        # Generated .env files target guest install root (/opt/homelab). For local
        # compose syntax checks, point INSTALL_ROOT at the repo so sidecar env files resolve.
        overrides = {"INSTALL_ROOT": str(root)}
        profiles = _compose_profiles_for_vm(cfg, vm)
        if profiles:
            overrides["COMPOSE_PROFILES"] = profiles
        from toolkit.core.compose.docker import compose_process_environment

        env = compose_process_environment(vm_env, overrides=overrides)
        from toolkit.core.infra.host_capacity import detect_host_capacity

        cap = detect_host_capacity(cfg=cfg, root=root)
        compose_timeout = min(90, max(45, cap.wave_timeout_s // 2))
        result = _run_subprocess(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--env-file",
                str(vm_env),
                "config",
                "--quiet",
            ],
            cwd=root,
            env=env,
            timeout=compose_timeout,
        )
        if result.returncode == 0:
            report.checks.append(f"docker compose config passed for {vm}")
        else:
            stderr = result.stderr.strip() or result.stdout.strip() or "unknown docker compose error"
            report.errors.append(f"docker compose config failed for {vm}: {stderr}")


def _skip_heavy_local_validation() -> bool:
    return os.environ.get("HOMELAB_DEPLOY_CONTROLLER", "").strip().lower() in ("1", "true", "yes")


def _validate_ansible(root: Path, report: ValidationReport) -> None:
    if _skip_heavy_local_validation():
        report.skipped.append("Ansible validation skipped: deploy controller")
        return
    from toolkit.core.ansible.ansible_ssh import resolve_tool

    ansible_dir = root / "automation" / "ansible"
    if not ansible_dir.is_dir():
        report.skipped.append("Ansible validation skipped: automation/ansible/ missing")
        return

    env = os.environ.copy()
    ansible_runtime = root / ".runtime" / "validation" / "ansible"
    local_tmp = ansible_runtime / "local"
    remote_tmp = ansible_runtime / "remote"
    local_tmp.mkdir(parents=True, exist_ok=True)
    remote_tmp.mkdir(parents=True, exist_ok=True)
    env["ANSIBLE_HOME"] = str(ansible_runtime)
    env["ANSIBLE_LOCAL_TEMP"] = str(local_tmp)
    env["ANSIBLE_REMOTE_TEMP"] = str(remote_tmp)
    venv_bin = root / ".venv" / "bin"
    resolved_ansible_playbook = resolve_tool("ansible-playbook", root)
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    elif resolved_ansible_playbook:
        tool_dir = str(Path(resolved_ansible_playbook).parent)
        env["PATH"] = f"{tool_dir}{os.pathsep}{env.get('PATH', '')}"

    ansible_playbook = resolved_ansible_playbook or "ansible-playbook"
    playbooks = [
        ansible_dir / "host-setup.yml",
        ansible_dir / "guest-setup.yml",
        ansible_dir / "site.yml",
    ]
    # Add all playbooks in playbooks/ dir
    playbooks_dir = ansible_dir / "playbooks"
    if playbooks_dir.is_dir():
        playbooks.extend(sorted(playbooks_dir.glob("*.yml")))
    inventory = ansible_dir / "inventory" / "hosts.yml"
    from toolkit.core.ansible.ansible_inventory import generated_extra_vars

    extra_vars = generated_extra_vars(root)

    all_ok = True
    for pb in playbooks:
        command = [ansible_playbook]
        if inventory.is_file():
            command.extend(["-i", str(inventory)])
        command.extend([*extra_vars, "--syntax-check", str(pb)])
        result = _run_subprocess(
            command,
            cwd=ansible_dir,
            env=env,
            timeout=60,
        )
        if result.returncode == 0:
            report.checks.append(f"ansible-playbook syntax: {pb.name} OK")
        else:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Ansible syntax error"
            report.errors.append(f"ansible-playbook syntax: {pb.name} FAILED: {detail[:500]}")
            all_ok = False

    # Validate inventory parsing
    if inventory.exists():
        result = _run_subprocess(
            ["ansible-inventory", "-i", str(inventory), "--list"],
            cwd=ansible_dir,
            env=env,
            timeout=30,
        )
        if result.returncode == 0:
            report.checks.append("ansible-inventory --list OK")
        else:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown inventory parse error"
            report.errors.append(f"ansible-inventory --list failed: {detail[:500]}")
    if not all_ok:
        report.errors.append("One or more Ansible playbook syntax checks failed")


def _validate_tofu(root: Path, report: ValidationReport) -> None:
    infra_dir = root / "infrastructure"
    if not infra_dir.exists():
        report.skipped.append("OpenTofu validation skipped: infrastructure/ missing")
        return
    if not shutil.which("tofu"):
        report.skipped.append("OpenTofu validation skipped: tofu not installed")
        return
    if not (infra_dir / ".terraform").exists() and not (infra_dir / ".tofu").exists():
        report.skipped.append("OpenTofu validation skipped: infrastructure not initialized")
        return
    result = _run_subprocess(["tofu", "validate"], cwd=infra_dir, timeout=120)
    if result.returncode == 0:
        report.checks.append("OpenTofu validate passed")
    else:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown OpenTofu validation error"
        report.errors.append(f"OpenTofu validate failed: {stderr}")


def validate_generated_artifacts(root: Path) -> ValidationReport:
    """Validate generated files and syntax-check deploy inputs."""
    root = root.resolve()
    report = ValidationReport()
    cfg = load_config(config_path(root))

    _validate_static_generated_files(root, cfg, report)
    _validate_cross_vm_ingress(root, cfg, report)
    _validate_env_files(root, cfg, report)
    _validate_compose(root, cfg, report)
    _validate_ansible(root, report)
    _validate_tofu(root, report)
    return report
