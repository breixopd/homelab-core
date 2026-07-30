"""Push deploy completion notifications via ntfy.sh (or any ntfy-compatible URL)."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from toolkit.core.config.config import Config
from toolkit.services.ntfy.client import normalize_ntfy_url, post_ntfy_url


def resolve_deploy_notify_url(cfg: Config, secrets: dict[str, str]) -> str:
    """Resolve ntfy POST URL from secrets or notifications config."""
    for key in ("DEPLOY_NTFY_URL", "NTFY_DEPLOY_URL"):
        url = (secrets.get(key) or "").strip()
        if url:
            return url
    notify = getattr(cfg, "notifications", None)
    if notify is not None:
        url = (getattr(notify, "deploy_ntfy_url", "") or "").strip()
        if url:
            return url
    return ""


def resolve_deploy_notify_post_url(cfg: Config, raw: str) -> str:
    """Use the private ntfy endpoint for this homelab's public topic URL."""
    normalized = normalize_ntfy_url(raw)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.hostname != f"ntfy.{cfg.domain}":
        return normalized

    from toolkit.services.ntfy.client import resolve_infra_ntfy_url

    topic = parsed.path.strip("/").split("/", 1)[0]
    if not topic:
        return ""
    return f"{resolve_infra_ntfy_url(cfg).rstrip('/')}/{topic}"


def resolve_deploy_notify_fallback_url(cfg: Config, post_url: str) -> str:
    """Return a local fallback for a configured ntfy.sh topic."""
    parsed = urlparse(post_url)
    if parsed.hostname != "ntfy.sh":
        return ""
    topic = parsed.path.strip("/").split("/", 1)[0]
    if not topic:
        return ""

    from toolkit.services.ntfy.client import resolve_infra_ntfy_url

    controller_role = os.environ.get("HOMELAB_CONTROLLER_ROLE", "").strip().lower()
    if controller_role == "local":
        base_url = "http://ntfy:80"
    else:
        base_url = os.environ.get("HOMELAB_NTFY_URL", "").strip() or resolve_infra_ntfy_url(cfg)
    return f"{base_url.rstrip('/')}/{topic}"


def build_deploy_notification_body(
    cfg: Config,
    *,
    success: bool,
    message: str,
    notification_type: str,
    step_status: dict[str, str],
    hook_summary: str = "",
    verify_summary: str = "",
) -> tuple[str, str, str]:
    """Return (title, message, priority) for ntfy. No secrets in output."""
    from toolkit.core.deploy.deploy_workflow import workflow_step_labels

    labels = workflow_step_labels(cfg)
    ok_steps = [labels.get(s, s) for s, st in step_status.items() if st == "ok"]
    fail_steps = [labels.get(s, s) for s, st in step_status.items() if st == "fail"]
    skip_steps = [labels.get(s, s) for s, st in step_status.items() if st == "skip"]

    if success:
        priority = "default"
        title = f"Deploy complete - {cfg.domain}"
    elif notification_type == "negative":
        priority = "high"
        title = f"Deploy failed - {cfg.domain}"
    else:
        priority = "default"
        title = f"Deploy finished with issues - {cfg.domain}"

    lines = [
        message,
        f"Domain: {cfg.domain}",
        f"Nodes: {', '.join(cfg.enabled_nodes)}",
    ]
    if ok_steps:
        lines.append(f"OK ({len(ok_steps)}): " + ", ".join(ok_steps[:8]) + ("…" if len(ok_steps) > 8 else ""))
    if fail_steps:
        lines.append(f"Failed ({len(fail_steps)}): " + ", ".join(fail_steps))
    if skip_steps:
        lines.append(f"Skipped ({len(skip_steps)}): " + ", ".join(skip_steps))
    if hook_summary:
        lines.append(hook_summary)
    if verify_summary:
        lines.append(verify_summary)
    lines.append("Check homelab-toolkit / UI for full logs.")

    return title, "\n".join(lines), priority


def send_deploy_notification(
    cfg: Config,
    secrets: dict[str, str],
    *,
    success: bool,
    message: str,
    notification_type: str,
    step_status: dict[str, str],
    hook_summary: str = "",
    verify_summary: str = "",
) -> bool:
    """POST deploy summary to ntfy. Returns True if sent successfully."""
    raw = resolve_deploy_notify_url(cfg, secrets)
    if not raw:
        return False
    post_url = resolve_deploy_notify_post_url(cfg, raw)
    if not post_url:
        return False

    title, body, priority = build_deploy_notification_body(
        cfg,
        success=success,
        message=message,
        notification_type=notification_type,
        step_status=step_status,
        hook_summary=hook_summary,
        verify_summary=verify_summary,
    )

    sent = post_ntfy_url(
        post_url,
        body,
        title=title,
        priority=priority,
        tags="white_check_mark" if success else "warning",
    )
    if sent:
        return True

    fallback_url = resolve_deploy_notify_fallback_url(cfg, post_url)
    if not fallback_url or fallback_url == post_url:
        return False
    return post_ntfy_url(
        fallback_url,
        body,
        title=title,
        priority=priority,
        tags="white_check_mark" if success else "warning",
        trust_env=False,
    )
