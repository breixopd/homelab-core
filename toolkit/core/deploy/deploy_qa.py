"""Post-deploy QA orchestration for framework and infrastructure checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.core.config.storage import secrets_path
from toolkit.core.ops.hook_verify import format_verify_report, verify_hooks
from toolkit.core.ops.verify import format_report, verify_all, verify_remote
from toolkit.core.secrets.secrets import load_secrets_plaintext


@dataclass
class QAResult:
    ok: bool
    sections: dict[str, bool] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)


def run_infrastructure_qa(
    root: Path,
    cfg: Config,
    *,
    vms: tuple[str, ...] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> QAResult:
    """Run infrastructure QA not already covered by deploy verification phases.

    The deploy workflow already runs `verify` (phase 8) and `verify_hooks`
    (phase 7) before calling this. Service plugins own their health and
    integration probes there. This phase only covers provider policy and
    custom-image presence on guests.

    Use `run_deploy_qa` instead for a standalone full QA pass (e.g. from
    the CLI `deploy verify --qa`), which includes verify + hooks + these
    infrastructure sections.
    """
    logs: list[str] = []
    sections: dict[str, bool] = {}

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    selected = set(vms or cfg.enabled_nodes)
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_node

    ingress_service = provider_service_name("ingress")
    if cfg.category_enabled("management") and cfg.dns.proxy_enabled and service_node(cfg, ingress_service) in selected:
        log("=== Cloudflare SSL mode ===")
        sections["cloudflare_ssl"] = _check_cloudflare_ssl(cfg, root, log)

    if cfg.is_multi_node:
        log("=== Custom images on guests ===")
        sections["custom_images"] = _check_custom_images(cfg, root, log, vms=vms)

    ok = all(sections.values()) if sections else True
    return QAResult(ok=ok, sections=sections, logs=logs)


def run_deploy_qa(
    root: Path,
    cfg: Config,
    *,
    vm: str | None = None,
    strict_hook_audit: bool = False,
    on_log: Callable[[str], None] | None = None,
) -> QAResult:
    logs: list[str] = []
    sections: dict[str, bool] = {}

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    secrets = load_secrets_plaintext(secrets_path(root))

    log("=== Container & URL verify ===")
    from toolkit.core.ansible.ansible_ssh import resolve_tool, should_verify_remote

    use_ansible_verify = should_verify_remote(cfg, root)
    if use_ansible_verify:
        verify_results = verify_remote(root, cfg, vm=vm)
    else:
        if cfg.is_multi_node and not resolve_tool("ansible", root):
            log("Skipping remote verify (ansible CLI not on PATH)")
        verify_results = verify_all(root, cfg, vm=vm)
    log(format_report(verify_results))
    sections["verify"] = all(r.ok for r in verify_results.values())

    log("=== Hook verify ===")
    hook_result = verify_hooks(cfg, secrets, root, vm=vm)
    log(format_verify_report(hook_result))
    sections["hooks"] = hook_result.all_passed

    if strict_hook_audit:
        from toolkit.core.deploy.hook_audit import strict_hooks_passed

        log("=== Strict post-start hook audit ===")
        audit_ok, audit_detail = strict_hooks_passed(root)
        log(f"Hook audit: {'clean' if audit_ok else 'not clean'} ({audit_detail})")
        sections["hook_audit"] = audit_ok

    extended = run_infrastructure_qa(root, cfg, vms=(vm,) if vm else None, on_log=on_log)
    sections.update(extended.sections)
    logs.extend(extended.logs)

    ok = all(sections.values()) if sections else False
    return QAResult(ok=ok, sections=sections, logs=logs)


def _check_cloudflare_ssl(cfg: Config, root: Path, log: Callable[[str], None]) -> bool:
    """Verify Cloudflare SSL/TLS mode is Full.

    This is a deployment gate whenever Cloudflare proxying is enabled.  A
    dashboard-managed or otherwise unverifiable setting is deliberately not
    treated as success: allowing an origin to deploy behind an unknown
    Cloudflare mode can result in a broken or plaintext edge connection.
    """
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.ops.dns import CloudflareDNS
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    token = secrets.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        log("Cloudflare SSL: unverified (Cloudflare API token is not configured)")
        return False

    client = CloudflareDNS(api_token=token, zone_id=secrets.get("CLOUDFLARE_ZONE_ID", ""))
    if not client._zone_id:
        client.find_zone_id(cfg.domain)

    try:
        current = client.get_zone_setting("ssl")
        log(f"Cloudflare SSL mode: {current}")
        if current == "full":
            return True
        if client.ensure_ssl_mode("full"):
            log("Cloudflare SSL/TLS mode set to Full")
            return True
        log("Cloudflare SSL: mode is not Full and token cannot update zone settings")
        return False
    except Exception:
        # Never include the exception text: provider clients may echo request
        # details, and the QA log is routinely persisted/shared.
        log("Cloudflare SSL: unverified (Cloudflare zone settings could not be read)")
        return False


def _check_custom_images(
    cfg: Config,
    root: Path,
    log: Callable[[str], None],
    *,
    vms: tuple[str, ...] | None = None,
) -> bool:
    """Verify custom images reconciled by the configured delivery policy."""
    from toolkit.core.images.publish import verify_guest_images

    ok, lines = verify_guest_images(
        cfg,
        root,
        registry=cfg.images.registry,
        tag=cfg.images.tag,
        vms=vms,
        on_log=log,
    )
    if not ok:
        log(
            "Custom images: run `homelab-toolkit images sync --source auto` "
            "to pull published images with a local-build fallback"
        )
    return ok
