"""Deploy verification — container health and optional HTTPS probes."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

from toolkit.core.compose.docker import DockerCompose, deployment_compose_path
from toolkit.core.compose.registry import load_all
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, config_path, env_path
from toolkit.core.state.paths import https_probe_cache_path, last_verify_path

# Per-URL HTTPS probe budget (controller-side). Probes run concurrently in verify_remote.
_HTTPS_PROBE_TIMEOUT = 6


def _https_probe_workers() -> int:
    import os

    if os.environ.get("HOMELAB_LOW_RESOURCE", "").strip().lower() in ("1", "true", "yes"):
        return 2
    return 8


_REMOTE_ANSIBLE_TIMEOUT = 300
_HTTPS_CACHE_TTL_SEC = 300


@dataclass(frozen=True, slots=True)
class RuntimeVerificationPolicy:
    owner: str
    mode: Literal["daemon", "oneshot"]
    starting_policy: Literal["fail", "pending"]
    completion_services: tuple[str, ...] = ()


def runtime_verification_policies(cfg: Config, node: str) -> dict[str, RuntimeVerificationPolicy]:
    """Compile service manifests into per-runtime verification behavior."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    policies: dict[str, RuntimeVerificationPolicy] = {}
    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest, catalog):
            continue
        daemon_runtimes = tuple(
            runtime
            for runtime, contract in manifest.runtimes.items()
            if contract.mode == "daemon" and node in manifest_runtime_nodes(cfg, manifest, runtime)
        )
        completion_services = tuple(dict.fromkeys((manifest.name, *daemon_runtimes)))
        if manifest_node(cfg, manifest) == node:
            policies[manifest.name] = RuntimeVerificationPolicy(
                owner=manifest.name,
                mode="daemon",
                starting_policy=manifest.health.starting_policy,
            )
        for runtime, contract in manifest.runtimes.items():
            if node not in manifest_runtime_nodes(cfg, manifest, runtime):
                continue
            policies[runtime] = RuntimeVerificationPolicy(
                owner=manifest.name,
                mode=contract.mode,
                starting_policy=manifest.health.starting_policy,
                completion_services=completion_services,
            )
    return policies


def pending_start_services(cfg: Config, node: str) -> frozenset[str]:
    """Return manifest-owned runtime names whose Docker starting state is non-fatal."""
    return frozenset(
        service
        for service, policy in runtime_verification_policies(cfg, node).items()
        if policy.mode == "daemon" and policy.starting_policy == "pending"
    )


def _https_cache_path(root: Path) -> Path:
    return https_probe_cache_path(root)


def _load_https_cache(root: Path) -> dict[str, tuple[bool, str, float]]:
    import time

    path = _https_cache_path(root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time()
    out: dict[str, tuple[bool, str, float]] = {}
    for url, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        # A failed reachability probe is only a point-in-time observation.
        # Reusing it makes a transient edge restart or DNS connection failure
        # poison every verification run for the full cache TTL.
        if not bool(entry.get("ok")):
            continue
        ts = float(entry.get("ts", 0))
        if now - ts > _HTTPS_CACHE_TTL_SEC:
            continue
        out[url] = (True, str(entry.get("detail", "")), ts)
    return out


def _save_https_cache(root: Path, cache: dict[str, tuple[bool, str, float]]) -> None:
    import time

    path = _https_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {url: {"ok": ok, "detail": detail, "ts": ts or time.time()} for url, (ok, detail, ts) in cache.items()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_last_verify_report(root: Path, results: dict[str, VerifyResult]) -> None:
    """Persist summary for dashboard (written after each verify run)."""
    path = last_verify_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        vm: {
            "ok": r.ok,
            "healthy": len(r.services_healthy),
            "unhealthy": len(r.services_unhealthy),
            "pending": len(r.services_pending),
            "errors": r.errors[:5],
        }
        for vm, r in results.items()
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class VerifyResult:
    vm: str
    docker_ok: bool = False
    compose_ok: bool = False
    services_healthy: list[str] = field(default_factory=list)
    services_unhealthy: list[str] = field(default_factory=list)
    services_pending: list[str] = field(default_factory=list)
    url_checks: list[tuple[str, bool, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.docker_ok
            and self.compose_ok
            and not self.errors
            and not self.services_unhealthy
            and all(url_ok for _, url_ok, _ in self.url_checks)
        )


def _docker_ps_native(*, timeout: int = 120) -> list:
    """Fast container listing via docker ps (avoids slow compose ps on busy hosts)."""
    from toolkit.core.compose.docker import ContainerStatus

    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    containers: list[ContainerStatus] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = data.get("Names", "") or data.get("Name", "")
        status = data.get("Status", "") or data.get("State", "")
        state = "running" if status.lower().startswith("up") else data.get("State", "unknown").lower()
        health = ""
        if "(" in status and ")" in status:
            inner = status.split("(", 1)[1].split(")", 1)[0].lower()
            if "unhealthy" in inner:
                health = "unhealthy"
            elif "starting" in inner:
                health = "starting"
            elif "healthy" in inner:
                health = "healthy"
        labels = data.get("Labels", "")
        compose_service = next(
            (label.split("=", 1)[1] for label in labels.split(",") if label.startswith("com.docker.compose.service=")),
            "",
        )
        container_name = name.lstrip("/")
        containers.append(
            ContainerStatus(
                name=container_name,
                service=compose_service or container_name,
                state=state,
                health=health,
                image=data.get("Image", ""),
            )
        )
    return containers


def _default_urls(cfg: Config) -> list[str]:
    """HTTPS probes from the controller — internet-exposed routes only."""
    if cfg.domain == "localhost":
        return []
    from toolkit.core.manifest.routes import public_routes

    urls: list[str] = []
    seen: set[str] = set()
    for route in public_routes(cfg):
        url = f"https://{route.host}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _one_shot_init_ok(
    svc: str,
    c,
    by_service: dict,
    policies: dict[str, RuntimeVerificationPolicy],
) -> bool:
    """One-shot jobs may exit or be removed after a declared daemon becomes healthy."""
    policy = policies.get(svc)
    if policy is None or policy.mode != "oneshot":
        return False
    if c is not None and c.state == "exited":
        return True
    if c is not None:
        return False
    # Container removed after successful init: an owner daemon is the durable signal.
    for service in policy.completion_services:
        peer = by_service.get(service)
        if peer and peer.state == "running" and peer.health in ("healthy", "", "starting"):
            return True
    return False


def _check_runtime_images(
    dc: DockerCompose, profiles: list[str], expected_services: list[str], containers: dict
) -> list[str]:
    """Fail closed when a running service is not using its rendered Compose image."""
    args: list[str] = []
    for profile in profiles:
        args.extend(["--profile", profile])
    args.extend(["config", "--format", "json"])
    try:
        proc = dc._run(args, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return ["docker compose config image inspection failed"]
    if proc.returncode != 0:
        return ["docker compose config image inspection failed"]
    try:
        services = json.loads(proc.stdout).get("services", {})
    except (json.JSONDecodeError, AttributeError):
        return ["docker compose config image inspection returned invalid JSON"]
    errors: list[str] = []
    for service in expected_services:
        container = containers.get(service)
        if container is None or container.state != "running":
            continue
        desired = str(services.get(service, {}).get("image", "")).strip()
        if not desired:
            continue
        loaded_ref = str(container.image).strip()
        try:
            inspect = subprocess.run(
                ["docker", "inspect", "--format", "{{.Image}}", container.name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            errors.append(f"{service}(image inspect failed)")
            continue
        if inspect.returncode != 0:
            errors.append(f"{service}(image inspect failed)")
            continue
        loaded_id = inspect.stdout.strip()
        if not loaded_id:
            errors.append(f"{service}(image inspect failed)")
            continue
        # Rendered references (including mutable tags and digest-pinned refs) are
        # accepted only when the running container's resolved ID matches the
        # locally resolved desired image ID.
        try:
            desired_inspect = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", desired],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            errors.append(f"{service}(image inspect failed)")
            continue
        desired_id = desired_inspect.stdout.strip()
        if desired_inspect.returncode != 0 or not desired_id:
            errors.append(f"{service}(image inspect failed)")
            continue
        if desired_id != loaded_id:
            errors.append(f"{service}(image mismatch desired={desired} loaded={loaded_ref})")
    return errors


def _check_url(url: str, timeout: int = _HTTPS_PROBE_TIMEOUT) -> tuple[bool, str]:
    from toolkit.core.net.http_probe import probe_url

    return probe_url(url, timeout=float(timeout))


def _check_url_via_ingress(cfg: Config, root: Path, url: str) -> tuple[bool, str]:
    """Probe a public hostname through the manifest-owned Caddy ingress."""
    from urllib.parse import urlparse

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address

    host = urlparse(url).hostname or ""
    if not host:
        return False, "invalid url"
    ingress_ip = service_address(cfg, provider_service_name("ingress"))
    cmd = (
        f"curl -sk -o /dev/null -w '%{{http_code}}' "
        f"--resolve {host}:443:127.0.0.1 "
        f"https://{host}/ --connect-timeout 5 2>/dev/null || "
        f"curl -sk -o /dev/null -w '%{{http_code}}' "
        f"--resolve {host}:443:{ingress_ip} "
        f"https://{host}/ --connect-timeout 5 2>/dev/null"
    )
    rc, out, err = ssh_run_on_vm(cfg, ingress_ip, cmd, root=root, timeout=20)
    code = (out or err or "").strip().splitlines()[-1] if (out or err) else ""
    code = code.replace("RC:", "").strip() or str(rc)
    if code in ("200", "301", "302", "401", "403"):
        return True, code
    return False, code or f"rc={rc}"


def _is_controller_transport_failure(detail: str) -> bool:
    """Return whether an edge probe failed before receiving an HTTP response."""
    normalized = detail.strip().lower()
    return any(
        marker in normalized
        for marker in (
            "connection refused",
            "connecterror",
            "timed out",
            "timeout",
            "network is unreachable",
            "no route to host",
            "name or service not known",
            "nodename",
            "temporary failure in name resolution",
        )
    )


def _check_urls(
    urls: list[str],
    *,
    timeout: int = _HTTPS_PROBE_TIMEOUT,
    root: Path | None = None,
    cfg: Config | None = None,
) -> list[tuple[str, bool, str]]:
    """Probe HTTPS URLs concurrently; preserve input order. Uses short-lived cache when *root* set."""
    if not urls:
        return []
    import time

    cache = _load_https_cache(root) if root is not None else {}
    to_probe: list[str] = []
    ordered: dict[str, tuple[bool, str]] = {}
    for url in urls:
        hit = cache.get(url)
        if hit is not None:
            ordered[url] = (hit[0], hit[1])
        else:
            to_probe.append(url)

    if to_probe:
        workers = min(_https_probe_workers(), len(to_probe))
        now = time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_check_url, url, timeout): url for url in to_probe}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    ok, detail = future.result()
                except Exception as exc:
                    ok, detail = False, str(exc)
                if (
                    not ok
                    and cfg is not None
                    and cfg.is_multi_node
                    and root is not None
                    and _is_controller_transport_failure(detail)
                ):
                    edge_detail = detail
                    ingress_ok, ingress_detail = _check_url_via_ingress(cfg, root, url)
                    if ingress_ok:
                        ok = True
                        detail = f"ingress {ingress_detail}; controller edge probe unavailable: {edge_detail}"
                ordered[url] = (ok, detail)
                cache[url] = (ok, detail, now)
        if root is not None:
            _save_https_cache(root, cache)

    return [(url, ordered[url][0], ordered[url][1]) for url in urls]


def verify_vm(root: Path, cfg: Config, vm: str, extra_urls: list[str] | None = None) -> VerifyResult:
    load_all()
    result = VerifyResult(vm=vm)
    env_file = env_path(vm, root)
    compose_file = deployment_compose_path(cfg, root, vm)

    if not compose_file.exists():
        result.errors.append(f"Compose model missing for {vm}: {compose_file}")
        return result

    if not env_file.exists():
        result.errors.append(f"generated/{vm}/.env missing")
        return result

    dc = DockerCompose(compose_file=compose_file, env_file=env_file)
    if not dc.preflight():
        result.errors.append("Docker daemon unreachable")
        return result
    result.docker_ok = True

    from toolkit.core.manifest.storage import read_role_environment

    profiles = sorted(
        profile for profile in read_role_environment(env_file).get("COMPOSE_PROFILES", "").split(",") if profile
    )
    config_args: list[str] = []
    for profile in profiles:
        config_args.extend(["--profile", profile])
    config_args.extend(["config", "--quiet"])
    proc = dc._run(config_args, timeout=300)
    if proc.returncode != 0:
        result.errors.append(proc.stderr.strip() or "docker compose config failed")
        return result
    result.compose_ok = True

    svc_args: list[str] = []
    for profile in profiles:
        svc_args.extend(["--profile", profile])
    svc_args.extend(["config", "--services"])
    svc_proc = dc._run(svc_args, timeout=300)
    if svc_proc.returncode != 0:
        result.errors.append(svc_proc.stderr.strip() or "docker compose config --services failed")
        return result
    expected = [line.strip() for line in svc_proc.stdout.splitlines() if line.strip()]

    ps_timeout = 300 if len(expected) > 15 else 120
    containers = _docker_ps_native(timeout=ps_timeout)
    if not containers:
        containers = dc.ps(timeout=ps_timeout)
    by_service = {c.service: c for c in containers}
    runtime_policies = runtime_verification_policies(cfg, vm)
    pending_starts = frozenset(
        service
        for service, policy in runtime_policies.items()
        if policy.mode == "daemon" and policy.starting_policy == "pending"
    )
    from toolkit.core.projects.placement import project_node

    for project in cfg.projects.entries:
        if project_node(cfg, project) == vm:
            container = by_service.get(project.subdomain)
            if container is not None:
                by_service[f"project-{project.subdomain}"] = container
    for svc in expected:
        c = by_service.get(svc)
        if not c:
            if _one_shot_init_ok(svc, c, by_service, runtime_policies):
                result.services_healthy.append(svc)
            else:
                result.services_unhealthy.append(f"{svc}(missing)")
            continue
        if c.state == "running" and c.health == "healthy":
            result.services_healthy.append(svc)
        elif c.state == "running" and c.health == "starting":
            if svc in pending_starts:
                result.services_pending.append(svc)
            else:
                result.services_unhealthy.append(f"{svc}(starting)")
        elif c.state == "running" and c.health == "":
            # Running without Docker health status — treat as healthy (no healthcheck configured).
            result.services_healthy.append(svc)
        elif _one_shot_init_ok(svc, c, by_service, runtime_policies):
            result.services_healthy.append(svc)
        else:
            result.services_unhealthy.append(f"{svc}({c.state}/{c.health})")

    result.errors.extend(_check_runtime_images(dc, profiles, expected, by_service))

    urls = list(extra_urls or [])
    for project in cfg.projects.entries:
        if project.health_endpoint and project_node(cfg, project) == vm:
            urls.append(f"http://{cfg.node_ip(vm)}:{project.container_port}{project.health_endpoint}")
    # HTTPS probes run on the controller only; guests often lack LAN DNS for app subdomains.
    if vm == cfg.control_node and not os.environ.get("HOMELAB_NODE"):
        urls.extend(_default_urls(cfg))

    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    result.url_checks.extend(_check_urls(unique_urls, root=root, cfg=cfg))

    return result


def verify_all(
    root: Path,
    cfg: Config | None = None,
    *,
    vm: str | None = None,
    extra_urls: list[str] | None = None,
) -> dict[str, VerifyResult]:
    cfg = cfg or load_config(config_path(root))
    targets = [vm] if vm else cfg.enabled_nodes
    results = {name: verify_vm(root, cfg, name, extra_urls) for name in targets}
    save_last_verify_report(root, results)
    return results


def _default_urls_extended(cfg: Config) -> list[str]:
    urls = _default_urls(cfg)
    if cfg.domain != "localhost":
        git_url = f"https://git.{cfg.domain}"
        if git_url not in urls:
            urls.append(git_url)
    return urls


def _parse_verify_json_payload(text: str, vm: str) -> VerifyResult | None:
    """Parse JSON emitted by homelab-toolkit deploy verify --json on a guest."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    vm_data = payload.get(vm)
    if not isinstance(vm_data, dict):
        return None
    result = VerifyResult(vm=vm)
    result.docker_ok = True
    result.compose_ok = True
    result.services_healthy = list(vm_data.get("healthy") or [])
    result.services_unhealthy = list(vm_data.get("unhealthy") or [])
    result.services_pending = list(vm_data.get("pending") or [])
    result.errors = list(vm_data.get("errors") or [])
    for entry in vm_data.get("urls") or []:
        if isinstance(entry, dict):
            result.url_checks.append((entry.get("url", ""), bool(entry.get("ok")), str(entry.get("detail", ""))))
    return result


def verify_remote(
    root: Path,
    cfg: Config,
    *,
    vm: str | None = None,
    extra_urls: list[str] | None = None,
) -> dict[str, VerifyResult]:
    """Run deployment verification on managed machines through Ansible."""
    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    if not inventory.exists():
        return verify_all(root, cfg, vm=vm, extra_urls=extra_urls)

    targets = [vm] if vm else cfg.enabled_nodes
    probe_urls = list(extra_urls or [])
    if not probe_urls and cfg.domain != "localhost":
        probe_urls = _default_urls_extended(cfg)

    def _verify_one(name: str) -> VerifyResult:
        result = VerifyResult(vm=name)
        host = cfg.machines[name].hostname
        repo = DEFAULT_HOMELAB_ROOT
        cmd = (
            f"export HOMELAB_NODE={name} HOMELAB_ROOT={repo}; "
            f"{repo}/.venv/bin/python3 -m toolkit.cli --root {repo} deploy verify --node {name} --json"
        )
        from toolkit.core.ansible.ansible_ssh import resolve_tool

        ansible_bin = resolve_tool("ansible", root) or "ansible"
        proc = subprocess.run(
            [
                ansible_bin,
                host,
                "-i",
                str(inventory),
                "-m",
                "shell",
                "-a",
                cmd,
            ],
            cwd=str(root / "automation" / "ansible"),
            capture_output=True,
            text=True,
            timeout=_REMOTE_ANSIBLE_TIMEOUT,
        )
        if proc.returncode != 0:
            result.errors.append(proc.stderr.strip() or proc.stdout.strip() or "ansible verify failed")
        else:
            parsed = _parse_verify_json_payload(proc.stdout, name)
            if parsed is not None:
                result = parsed
            else:
                result.errors.append("remote verify returned an invalid JSON payload")
        return result

    results: dict[str, VerifyResult] = {}
    if len(targets) == 1:
        results[targets[0]] = _verify_one(targets[0])
    else:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {pool.submit(_verify_one, name): name for name in targets}
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()

    if probe_urls:
        control = results.get(cfg.control_node)
        if control is not None:
            seen_urls = {u for u, _, _ in control.url_checks}
            pending = [url for url in probe_urls if url not in seen_urls]
            control.url_checks.extend(_check_urls(pending, root=root, cfg=cfg))
    save_last_verify_report(root, results)
    return results


def format_report(results: dict[str, VerifyResult]) -> str:
    lines: list[str] = []
    for vm, r in results.items():
        status = "OK" if r.ok else "FAIL"
        lines.append(f"\n=== {vm} [{status}] ===")
        lines.append(f"  Docker: {'yes' if r.docker_ok else 'no'}")
        lines.append(f"  Healthy: {len(r.services_healthy)}  Unhealthy: {len(r.services_unhealthy)}")
        if r.services_pending:
            lines.append(f"  Pending: {len(r.services_pending)} (starting up, no healthcheck yet)")
        if r.services_unhealthy:
            lines.append(f"  Issues: {', '.join(r.services_unhealthy[:12])}")
        for url, ok, detail in r.url_checks:
            mark = "✓" if ok else "✗"
            lines.append(f"  {mark} {url} ({detail})")
        for err in r.errors:
            lines.append(f"  ✗ {err}")
    return "\n".join(lines).strip()


def verify_sso(cfg: Config, *, root: Path | None = None) -> dict:
    """Check Authelia OIDC discovery endpoint and issuer alignment."""
    domain = cfg.domain
    if not domain or domain == "localhost":
        return {"ok": False, "checks": [], "error": "domain is localhost"}
    from toolkit.services.sdk import authelia_oidc_issuer

    issuer = authelia_oidc_issuer(cfg)
    discovery = f"{issuer}/.well-known/openid-configuration"
    checks: list[dict] = []
    ok = True
    body: dict = {}
    try:
        if cfg.is_multi_node and root is not None:
            from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
            from toolkit.core.manifest.catalog import provider_service_name
            from toolkit.core.manifest.placement import service_address

            ingress_ip = service_address(cfg, provider_service_name("ingress"))
            cmd = (
                f"curl -sk --resolve auth.{domain}:443:127.0.0.1 https://auth.{domain}/.well-known/openid-configuration"
            )
            rc, out, err = ssh_run_on_vm(cfg, ingress_ip, cmd, root=root, timeout=20)
            if rc != 0 or not out.strip():
                raise URLError(err or f"rc={rc}")
            body = json.loads(out)
            checks.append({"name": "oidc_discovery", "ok": True, "url": discovery})
        else:
            req = Request(discovery, headers={"Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
            checks.append({"name": "oidc_discovery", "ok": True, "url": discovery})
        if body.get("issuer") != issuer:
            ok = False
            checks.append({"name": "issuer_match", "ok": False, "expected": issuer, "got": body.get("issuer")})
        else:
            checks.append({"name": "issuer_match", "ok": True})
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            present = bool(body.get(key))
            checks.append({"name": key, "ok": present})
            ok = ok and present
    except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        ok = False
        checks.append({"name": "oidc_discovery", "ok": False, "url": discovery, "error": str(exc)})
    return {"ok": ok, "issuer": issuer, "checks": checks}


def format_sso_report(report: dict) -> str:
    lines = [f"SSO verify: {'OK' if report.get('ok') else 'FAILED'}", f"Issuer: {report.get('issuer', '')}"]
    for check in report.get("checks", []):
        mark = "✓" if check.get("ok") else "✗"
        name = check.get("name", "")
        extra = check.get("error") or check.get("got") or ""
        lines.append(f"  {mark} {name} {extra}".rstrip())
    return "\n".join(lines)
