"""Post-deploy guidance — only prerequisites and steps automation cannot perform.

Automated setup (hooks + verify): Immich, Vaultwarden, Gitea, Jellyfin, qBit, LLDAP,
*arr/Prowlarr/Tdarr, Seerr, Headscale preauth, AdGuard, mail DKIM, Kopia repo, etc.
Use `homelab-toolkit deploy verify --qa --strict` to confirm the stack.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog


@dataclass
class ManualStep:
    service: str
    title: str
    instructions: str
    url: str = ""
    category: str = "Required"  # Required | Verify | Optional | Prerequisite
    hook_failed: bool = False


def _available_secrets(values: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return secret presence information without ever exposing secret values.

    Deploy callers can pass the already-loaded secret mapping.  The fallback is
    intentionally best-effort for dashboard/CLI callers that only have a
    ``Config`` object: environment values and the local encrypted store are
    consulted, while unreadable stores simply behave as empty.
    """
    if values is not None:
        return values

    result = {name: value for name, value in os.environ.items() if value}
    try:
        from toolkit.core.config.storage import resolve_homelab_root, secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext

        path = secrets_path(resolve_homelab_root(prefer_cwd=True))
        if path.is_file():
            result.update({name: value for name, value in load_secrets_plaintext(path).items() if value})
    except Exception:
        # Guidance must never make deploy/dashboard rendering fail because a
        # secret store is unavailable; preflight remains responsible for that.
        pass
    return result


def _service_guidance(
    config: Config,
    phase: str,
    catalog: ServiceCatalog | None = None,
    secrets: Mapping[str, str] | None = None,
) -> list[ManualStep]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes, service_is_enabled
    from toolkit.core.manifest.setup import active_setup_secrets

    selected = catalog or load_service_catalog()
    default_routes = {route.service: route for route in compile_routes(config, selected) if route.match is None}
    available = _available_secrets(secrets)
    active_secrets = active_setup_secrets(config, selected)
    steps: list[ManualStep] = []
    for manifest in selected.manifests:
        if not service_is_enabled(config, manifest, selected):
            continue
        active_user_secrets = {
            name
            for name, (owner, secret) in active_secrets.items()
            if owner.name == manifest.name and secret.tier == "user"
        }
        for entry in manifest.guidance:
            if entry.phase != phase:
                continue
            # A pre-deploy prompt is complete once all active user-owned
            # secrets for that service are present.  This keeps successful
            # deploy recaps focused on real work (and handles conditional
            # secrets such as the selected VPN provider).
            if (
                phase == "pre_deploy"
                and active_user_secrets
                and all(str(available.get(name, "")).strip() for name in active_user_secrets)
            ):
                continue
            route = default_routes.get(manifest.name)
            url = ""
            if entry.route_url:
                if route is None:
                    raise ValueError(f"enabled service {manifest.name!r} has no compiled default route")
                scheme = "http" if config.domain == "localhost" else "https"
                url = f"{scheme}://{route.host}"
            steps.append(
                ManualStep(
                    service=manifest.name,
                    title=entry.title,
                    instructions=entry.instructions.replace("{domain}", config.domain).replace("{url}", url),
                    url=url,
                    category=entry.category,
                )
            )
    return steps


def get_prerequisite_steps(
    config: Config,
    *,
    catalog: ServiceCatalog | None = None,
    secrets: Mapping[str, str] | None = None,
) -> list[ManualStep]:
    """Secrets and one-time values you must supply before the first successful deploy."""
    steps: list[ManualStep] = []
    available = _available_secrets(secrets)
    if config.dns.provider == "cloudflare" and not all(
        str(available.get(name, "")).strip() for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID")
    ):
        steps.append(
            ManualStep(
                service="cloudflare",
                title="Cloudflare API token and zone ID",
                instructions=(
                    "Run `homelab-toolkit secrets set CLOUDFLARE_API_TOKEN` and "
                    "`homelab-toolkit secrets set CLOUDFLARE_ZONE_ID` (or use the setup wizard). "
                    "Required for public DNS sync via `homelab-toolkit dns sync`."
                ),
                category="Prerequisite",
            )
        )

    if config.proxmox.provision_machines and not all(
        str(available.get(name, "")).strip() for name in ("PROXMOX_API_TOKEN_ID", "PROXMOX_API_TOKEN_SECRET")
    ):
        steps.append(
            ManualStep(
                service="proxmox",
                title="Proxmox API token",
                instructions=(
                    "Run `homelab-toolkit secrets set PROXMOX_API_TOKEN_ID` and "
                    "`homelab-toolkit secrets set PROXMOX_API_TOKEN_SECRET`. "
                    "LXCs need proxmox.ssh_public_key in config.local.yaml and the "
                    "matching private key at ssh.key_file (setup writes both)."
                ),
                category="Prerequisite",
            )
        )

    return steps + _service_guidance(config, "pre_deploy", catalog, available)


def get_manual_steps(
    config: Config,
    hook_results: dict[str, list[str]] | None = None,
    *,
    catalog: ServiceCatalog | None = None,
    secrets: Mapping[str, str] | None = None,
) -> list[ManualStep]:
    """Return steps that still require a human after deploy (excludes hook-verified services)."""
    hook_results = hook_results or {}
    steps: list[ManualStep] = []

    failed_cats = [cat for cat, logs in hook_results.items() if any(line.startswith("Hook error:") for line in logs)]
    if failed_cats:
        steps.append(
            ManualStep(
                service="recovery",
                title="Recover failed post-start hooks",
                instructions=(
                    f"Categories with errors: {', '.join(failed_cats)}. "
                    "On a guest: `homelab-toolkit deploy hooks --node <role>`. "
                    "Or from controller: `homelab-toolkit deploy recover` then re-run verify."
                ),
                category="Required",
            )
        )

    return steps + _service_guidance(config, "post_deploy", catalog, secrets)


def get_all_manual_guidance(
    config: Config,
    hook_results: dict[str, list[str]] | None = None,
    *,
    catalog: ServiceCatalog | None = None,
    secrets: Mapping[str, str] | None = None,
) -> list[ManualStep]:
    """Prerequisites + unavoidable post-deploy steps."""
    return get_prerequisite_steps(config, catalog=catalog, secrets=secrets) + get_manual_steps(
        config,
        hook_results,
        catalog=catalog,
        secrets=secrets,
    )


def format_manual_steps_cli(steps: list[ManualStep]) -> str:
    """Format manual steps for CLI output."""
    lines: list[str] = []
    by_cat: dict[str, list[ManualStep]] = {
        "Prerequisite": [],
        "Required": [],
        "Verify": [],
        "Optional": [],
    }
    for step in steps:
        by_cat.setdefault(step.category, []).append(step)

    for cat_label, cat_key in [
        ("Before first deploy (you must provide)", "Prerequisite"),
        ("Required after deploy", "Required"),
        ("Verify (automation ran — confirm in UI)", "Verify"),
        ("Optional", "Optional"),
    ]:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        lines.append(f"\n{cat_label}:")
        for step in items:
            lines.append(f"  • [{step.service}] {step.title}")
            for part in step.instructions.split(". "):
                if part.strip():
                    lines.append(f"    {part.strip()}{'' if part.endswith('.') else '.'}")
            if step.url:
                lines.append(f"    → {step.url}")

    if not lines:
        return "No manual steps — automation and verify gates cover enabled services."
    return "\n".join(lines).strip()
