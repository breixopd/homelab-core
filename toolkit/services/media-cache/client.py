from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from toolkit.core.config.config import ExternalHost, load_config
from toolkit.core.config.storage import config_path

QueryParamValue = str | int | float | bool | None


@dataclass
class CacheStats:
    total_files: int = 0
    cache_size_gb: float = 0.0
    cache_used_pct: float = 0.0
    active_prefetch: int = 0
    cold_after_days: int = 0
    effective_uplink_mbps: float = 0.0
    configured_uplink_mbps: int = 0
    observed_uplink_mbps: float = 0.0
    time_to_first_frame_seconds: float = 0.0
    episode_fetch_seconds: int = 0
    movie_fetch_seconds: int = 0
    max_concurrent_4k: int = 0
    max_concurrent_1080p: int = 0
    bandwidth_source: str = "default"
    observed_samples: int = 0
    backends: list[str] | None = None


MEDIA_CACHE_URL = "http://media-cache:8686"


def resolve_media_cache_token(root: Path | None = None) -> str:
    """Resolve the media-cache admin token.

    Prefers ``MEDIA_CACHE_TOKEN`` from the environment (set inside containers on the
    media network); falls back to the encrypted secrets file when a homelab root is
    available (host-side hooks/CLI that load secrets into a dict, not os.environ).
    """
    token = os.environ.get("MEDIA_CACHE_TOKEN", "")
    if token or root is None:
        return token
    try:
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext

        return load_secrets_plaintext(secrets_path(root)).get("MEDIA_CACHE_TOKEN", "")
    except Exception:
        return ""


def media_cache_client(root: Path | None = None, base_url: str = MEDIA_CACHE_URL) -> MediaCacheClient:
    """Build a MediaCacheClient with the admin token resolved from env or secrets."""
    return MediaCacheClient(base_url, token=resolve_media_cache_token(root))


class MediaCacheClient:
    """Client for the media-cache promotion and cold-media demotion service."""

    def __init__(self, base_url: str = MEDIA_CACHE_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        # Shared secret for the mutating cache endpoints (pin/unpin).
        self.token = token if token is not None else os.environ.get("MEDIA_CACHE_TOKEN", "")

    def _admin_headers(self) -> dict[str, str]:
        return {"X-Media-Cache-Token": self.token} if self.token else {}

    def stats(self) -> CacheStats:
        try:
            resp = httpx.get(f"{self.base_url}/api/status", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            cache_bytes = data.get("cache_used_bytes", 0)
            bandwidth = data.get("bandwidth", {}) if isinstance(data.get("bandwidth"), dict) else {}
            return CacheStats(
                total_files=data.get("tracked_files", 0),
                cache_size_gb=round(cache_bytes / (1024**3), 2),
                cache_used_pct=float(data.get("cache_used_pct", 0.0)),
                active_prefetch=int(data.get("active_prefetch", 0)),
                cold_after_days=int(data.get("cold_after_days", 0)),
                effective_uplink_mbps=float(bandwidth.get("effective_uplink_mbps", 0.0)),
                configured_uplink_mbps=int(bandwidth.get("configured_uplink_mbps", 0)),
                observed_uplink_mbps=float(bandwidth.get("observed_uplink_mbps", 0.0)),
                time_to_first_frame_seconds=float(bandwidth.get("time_to_first_frame_seconds", 0.0)),
                episode_fetch_seconds=int(bandwidth.get("episode_fetch_seconds", 0)),
                movie_fetch_seconds=int(bandwidth.get("movie_fetch_seconds", 0)),
                max_concurrent_4k=int(bandwidth.get("max_concurrent_4k", 0)),
                max_concurrent_1080p=int(bandwidth.get("max_concurrent_1080p", 0)),
                bandwidth_source=str(bandwidth.get("source", "default")),
                observed_samples=int(bandwidth.get("observed_samples", 0)),
            )
        except httpx.HTTPError:
            return CacheStats()

    def health(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def backends(self) -> list[dict]:
        """List configured rclone remotes."""
        try:
            resp = httpx.get(f"{self.base_url}/api/backends", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            remotes = data.get("backends", [])
            return [{"name": r, "path": f"{r}:"} for r in remotes]
        except httpx.HTTPError:
            return []

    def webhook_url(self, source: str) -> str:
        """Return the inbound webhook URL for a given source (jellyfin, plex, tautulli)."""
        return f"{self.base_url}/webhook/{source}"

    def pin(self, path: str) -> bool:
        """Pin a library file so media-cache will not evict it."""
        try:
            resp = httpx.post(
                f"{self.base_url}/api/pin",
                json={"path": path},
                headers=self._admin_headers(),
                timeout=15,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def unpin(self, path: str) -> bool:
        """Remove transcode pin from a library file."""
        try:
            resp = httpx.post(
                f"{self.base_url}/api/unpin",
                json={"path": path},
                headers=self._admin_headers(),
                timeout=15,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


# ── Tautulli webhook registration ──────────────────────────────────────────
#
# Tautulli (https://github.com/Tautulli/Tautulli) is the recommended path for
# Plex users: it runs inside the media docker network and can reach
# ``http://media-cache:8686/webhook/tautulli`` directly, which Plex's
# account-level webhooks cannot (Plex cloud posts to a public URL only).
# The Tautulli API is documented at
# https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference.

TAUTULLI_WEBHOOK_AGENT_ID = 25


def _tautulli_webhook_body_template() -> str:
    """JSON body the Tautulli webhook notifier will POST to media-cache.

    Tautulli substitutes ``{param}`` tokens at send time (see
    ``build_media_notify_params`` in Tautulli's notification_handler.py).
    The shape matches the media-cache service image's ``/webhook/tautulli``
    expects: a top-level ``event`` plus a ``Metadata`` object.
    """
    return (
        '{"event": "{action}", '
        '"Metadata": {"type": "{media_type}", '
        '"title": "{title}", '
        '"grandparentTitle": "{show_name}", '
        '"parentIndex": "{season_num}", '
        '"index": "{episode_num}"}}'
    )


def register_tautulli_webhook(
    *,
    tautulli_url: str,
    api_key: str,
    webhook_url: str,
    friendly_name: str = "media-cache",
) -> tuple[bool, str]:
    """Idempotently register media-cache as a Tautulli webhook notifier.

    Returns ``(ok, message)``. ``ok`` is True when the notifier exists and
    points at ``webhook_url`` after the call (either it was already there, or
    we created/configured it). Best-effort: never raises — callers should log
    the returned message.
    """
    base = tautulli_url.rstrip("/")

    def _query(cmd: str, extra: dict[str, object]) -> dict[str, QueryParamValue]:
        query: dict[str, QueryParamValue] = {"apikey": api_key, "cmd": cmd}
        for key, value in extra.items():
            if isinstance(value, str | int | float | bool) or value is None:
                query[key] = value
            else:
                query[key] = str(value)
        return query

    def _cmd(cmd: str, **extra: object) -> dict | list | None:
        """Run a Tautulli API command. Returns ``data`` on success, ``None`` on failure.

        Tautulli wraps responses as ``{"response": {"result": "success", "data": ...}}``.
        ``set_notifier_config`` returns ``null`` data on success, so callers that
        care about success-vs-failure should use ``_cmd_ok`` instead.
        """
        query = _query(cmd, extra)
        try:
            resp = httpx.get(f"{base}/api/v2", params=query, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(data, dict) and data.get("response", {}).get("result") == "success":
            return data["response"].get("data")
        return None

    def _cmd_ok(cmd: str, **extra: object) -> bool:
        """True when Tautulli reports ``result=success`` (regardless of data)."""
        query = _query(cmd, extra)
        try:
            resp = httpx.get(f"{base}/api/v2", params=query, timeout=15)
            if resp.status_code != 200:
                return False
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return False
        return isinstance(data, dict) and data.get("response", {}).get("result") == "success"

    def _configure(notifier_id: int) -> bool:
        body = _tautulli_webhook_body_template()
        return _cmd_ok(
            "set_notifier_config",
            notifier_id=notifier_id,
            agent_id=TAUTULLI_WEBHOOK_AGENT_ID,
            webhook_hook=webhook_url,
            webhook_method="POST",
            on_play=1,
            on_resume=1,
            on_play_subject="",
            on_play_body=body,
            on_resume_subject="",
            on_resume_body=body,
            friendly_name=friendly_name,
        )

    # 1. Find an existing webhook notifier pointing at our URL (or our friendly_name).
    notifiers = _cmd("get_notifiers")
    if isinstance(notifiers, list):
        for entry in notifiers:
            if not isinstance(entry, dict) or entry.get("agent_id") != TAUTULLI_WEBHOOK_AGENT_ID:
                continue
            notifier_id = entry.get("id")
            if not notifier_id:
                continue
            cfg = _cmd("get_notifier_config", notifier_id=notifier_id)
            if not isinstance(cfg, dict):
                continue
            if cfg.get("config", {}).get("hook") == webhook_url:
                return True, f"tautulli webhook already registered (notifier {notifier_id})"
            if cfg.get("friendly_name") == friendly_name:
                if _configure(notifier_id):
                    return True, f"tautulli webhook reconfigured (notifier {notifier_id})"
                return False, f"tautulli notifier {notifier_id} reconfigure failed"

    # 2. No existing match — create a fresh webhook notifier. add_notifier_config
    # returns null data on success, so re-list to discover the new id.
    if not _cmd_ok("add_notifier_config", agent_id=TAUTULLI_WEBHOOK_AGENT_ID):
        return False, "tautulli add_notifier_config failed"

    notifiers = _cmd("get_notifiers") or []
    webhook_entries = [n for n in notifiers if isinstance(n, dict) and n.get("agent_id") == TAUTULLI_WEBHOOK_AGENT_ID]
    new_id = webhook_entries[-1].get("id") if webhook_entries else None
    if not new_id:
        return False, "tautulli add_notifier_config returned no notifier id"

    if _configure(new_id):
        return True, f"tautulli webhook registered (notifier {new_id})"
    return False, f"tautulli notifier {new_id} configure failed"


def external_media_cache_remote_name(host: ExternalHost) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", host.name).strip("-") or "host"
    return f"ext-{safe}"


def _rclone_union_upstream(remote_name: str, path: str) -> str:
    """Serialize one union upstream using rclone's quoted-list syntax."""
    value = f"{remote_name}:{path}"
    if not any(character.isspace() or character in {'"', "\\"} for character in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _obscure_rclone_secret(value: str) -> str:
    """Return rclone's encrypted representation of a secret.

    Rclone deliberately owns this format, so the controller never reimplements
    its obfuscation algorithm or writes an SSH password in plaintext.  The
    password is supplied over stdin because command arguments are observable
    through the process table. The binary is normally installed with the
    deployment toolchain; a clear error is returned when password-authenticated
    storage is selected without it.
    """
    executable = shutil.which("rclone")
    if not executable:
        raise RuntimeError("rclone is required to project password-authenticated media storage")
    try:
        result = subprocess.run(
            [executable, "obscure", "-"],
            input=f"{value}\n",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("rclone secret projection failed") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("rclone secret projection failed")
    return result.stdout.strip()


def _atomic_write_private(path: Path, content: str) -> None:
    """Atomically replace a mode-0600 text file without exposing secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _copy_private_key(source: Path, destination: Path) -> None:
    key_text = source.read_text(encoding="utf-8")
    if not key_text.endswith("\n"):
        key_text += "\n"
    _atomic_write_private(destination, key_text)


def _external_media_cache_hosts(cfg, *, excluded_names: set[str] | None = None) -> list[ExternalHost]:
    excluded = excluded_names or set()
    return sorted(
        (host for host in cfg.external_hosts if host.name not in excluded and "media-cache" in host.services),
        key=lambda host: host.name,
    )


def render_external_media_cache_config(
    root: Path,
    *,
    excluded_names: set[str] | None = None,
) -> tuple[Path, int]:
    """Project managed external hosts into the rclone configuration.

    The controller is the sole owner of this file.  The media-cache image only
    reads the resulting remotes, while the rclone container mounts the same
    immutable configuration.  A no-host desired state removes the config and
    stale generated keys so disabling an integration is deterministic.
    """
    cfg = load_config(config_path(root))
    hosts = _external_media_cache_hosts(cfg, excluded_names=excluded_names)
    config_dir = root / "config" / "rclone"
    config_path_value = config_dir / "rclone.conf"
    keys_dir = config_dir / "keys"

    if not hosts:
        config_path_value.unlink(missing_ok=True)
        if keys_dir.is_dir():
            for candidate in keys_dir.glob("ext-*.pem"):
                candidate.unlink(missing_ok=True)
        return config_path_value, 0

    sections: list[str] = []
    upstreams: list[str] = []
    desired_keys: set[str] = set()
    remote_owners: dict[str, str] = {}
    for host in hosts:
        fields = build_external_media_cache_sftp_fields(root, host)
        remote_name = external_media_cache_remote_name(host)
        remote_key = remote_name.casefold()
        previous_owner = remote_owners.get(remote_key)
        if previous_owner is not None:
            raise ValueError(
                f"Media cache hosts {previous_owner!r} and {host.name!r} generate the same rclone remote "
                f"{remote_name!r}"
            )
        remote_owners[remote_key] = host.name
        section = [
            f"[{remote_name}]",
            "type = sftp",
            f"host = {fields['host']}",
            f"user = {fields['user']}",
            f"port = {fields['port']}",
        ]
        if "pass" in fields:
            section.append(f"pass = {_obscure_rclone_secret(fields['pass'])}")
        else:
            key_name = Path(fields["key_file"]).name
            desired_keys.add(key_name)
            section.append(f"key_file = /config/rclone/keys/{key_name}")
        sections.append("\n".join(section))
        upstreams.append(_rclone_union_upstream(remote_name, fields["path"]))

    sections.append(
        "\n".join(
            [
                "[media-union]",
                "type = union",
                f"upstreams = {' '.join(upstreams)}",
                "action_policy = ff",
                "create_policy = ff",
                "search_policy = ff",
            ]
        )
    )
    _atomic_write_private(config_path_value, "\n\n".join(sections) + "\n")
    if keys_dir.is_dir():
        for candidate in keys_dir.glob("ext-*.pem"):
            if candidate.name not in desired_keys:
                candidate.unlink(missing_ok=True)
    return config_path_value, len(hosts)


def build_external_media_cache_sftp_fields(root: Path, host: ExternalHost) -> dict[str, str]:
    """Return validated SFTP fields for one managed storage host.

    This helper is intentionally side-effect free for password auth and only
    copies a key into the controller-owned rclone key directory for key auth.
    Runtime reconciliation is performed by :func:`render_external_media_cache_config`.
    """
    cfg = load_config(config_path(root))
    media_path = host.integration_value("media-cache", "path")
    if not isinstance(media_path, str) or not media_path.strip():
        raise ValueError("Media cache storage path is required for hosts in the storage pool")
    params: dict[str, str] = {
        "host": host.ip,
        "user": host.ssh_user,
        "port": str(host.ssh_port),
        "path": media_path.strip(),
    }

    if cfg.ssh.auth_method == "password":
        if not cfg.ssh.password:
            raise ValueError("Global SSH password is required for media-cache storage")
        # Keep the value available to the caller that projects the rclone file;
        # it must be obscured before being written to disk.
        params["pass"] = cfg.ssh.password
        return params

    if not cfg.ssh.key_file:
        raise ValueError("Global SSH key file is required for media-cache storage")

    source_key = Path(cfg.ssh.key_file).expanduser()
    if not source_key.exists():
        raise ValueError(f"SSH key file not found: {source_key}")

    keys_dir = root / "config" / "rclone" / "keys"
    dest_key = keys_dir / f"{external_media_cache_remote_name(host)}.pem"
    _copy_private_key(source_key, dest_key)
    params["key_file"] = f"/config/rclone/keys/{dest_key.name}"
    return params
