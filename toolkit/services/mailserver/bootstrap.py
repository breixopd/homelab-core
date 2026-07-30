"""Docker-Mailserver (DMS) bootstrap — DKIM generation + first-run checks.

DMS itself configures Postfix+Dovecot+Rspamd from env vars at container start.
This module handles the two things bootstrap needs to do *outside* the
container:

1. ``generate_dkim_keys()`` — run ``setup config dkim`` over SSH on the infra
   LXC the first time the mailserver starts. Writes ``<domain>.<selector>.txt``
   into ``/tmp/docker-mailserver/opendkim/`` (via the config bind-mount).

2. ``fetch_dms_dkim_txt(domain)`` — read the public DKIM TXT record DMS wrote
   when it generated keys. Used by ``dns.py`` to publish via Cloudflare and by
   ``hook_verify.py`` to compare against the live DNS record.

3. ``bootstrap_dms_mail(config, secrets)`` — the plugin hook entry point.
   Idempotent, so re-runs are cheap.
   This is the LLDAP-SASL auth sanity check that authenticated mail needs.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

logger = logging.getLogger(__name__)

#: Default OpenDKIM selector used by DMS (encrypted via RSA-2048 by default).
DEFAULT_DKIM_SELECTOR = "mail"

#: DMS CLI helper inside the container.
DMS_SETUP_CMD = "setup"


def _mail_ssh_exec(
    cfg: Config,
    cmd: str,
    *,
    root=None,
    timeout: int = 90,
) -> tuple[int, str, str]:
    """Run a shell command on the machine hosting mailserver."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    return ssh_run_on_vm(cfg, service_address(cfg, "mailserver"), cmd, root=root, timeout=timeout)


def _container_running(cfg: Config, container: str = "mailserver") -> bool:
    """Return True when the named container is running on the mail node."""
    rc, out, _ = _mail_ssh_exec(cfg, f"docker inspect -f '{{{{.State.Running}}}}' {container} 2>&1", timeout=10)
    return rc == 0 and (out or "").strip() == "true"


def generate_dkim_keys(cfg: Config) -> list[str]:
    """Tell DMS (Rspamd) to generate DKIM keys for ``cfg.domain`` if none exist.

    DMS 15.x (Rspamd) stores keys as
    ``/tmp/docker-mailserver/rspamd/dkim/rsa-2048-<selector>-<domain>.public.dns.txt``.
    Idempotent — skips generation if the public key file exists.
    """
    logs: list[str] = []
    if not cfg.is_multi_node or not cfg.category_enabled("email"):
        return logs
    if not _container_running(cfg):
        logs.append("DMS: mailserver container not running yet — skip DKIM")
        return logs

    domain = cfg.domain
    dkim_dir = "/opt/homelab/data/dms/config/rspamd/dkim"
    txt_path = f"{dkim_dir}/rsa-2048-{DEFAULT_DKIM_SELECTOR}-{domain}.public.dns.txt"

    # Skip if DKIM TXT already exists.
    rc, out, _ = _mail_ssh_exec(cfg, f"test -s {shlex.quote(txt_path)} && echo EXISTS", timeout=5)
    if "EXISTS" in (out or ""):
        logs.append(f"DMS: DKIM key for {domain} already present")
        return logs

    # Generate via Rspamd (DMS 15.x: `setup config dkim [keysize N] [domain D] [selector S]`).
    cmd = f"docker exec mailserver {DMS_SETUP_CMD} config dkim keysize 2048 domain {shlex.quote(domain)}"
    rc, out, err = _mail_ssh_exec(cfg, cmd, timeout=120)
    combined = (err or "") + (out or "")
    if rc != 0 and "not overwriting" in combined.lower():
        logs.append(f"DMS: DKIM key for {domain} already present")
    elif rc != 0:
        logs.append(f"DMS: DKIM generation warning ({(err or out)[:80]})")
    else:
        logs.append(f"DMS: DKIM keys generated ({domain}, selector={DEFAULT_DKIM_SELECTOR}, rsa-2048)")
    return logs


def fetch_dms_dkim_txt(domain: str, *, cfg: Config | None = None) -> str:
    """Return the DKIM TXT record DMS published for ``domain``.

    Reads from Rspamd's DKIM directory — DMS 15.x naming is
    ``rsa-2048-<selector>-<domain>.public.dns.txt`` (falls back to any
    ``*<domain>*.public*.txt`` match).
    Returns the full ``v=DKIM1; k=rsa; p=<base64>`` string for Cloudflare, or
    ``""`` when no key exists yet (callers treat empty as "skip DNS sync").
    """
    if cfg is None or not cfg.is_multi_node or not cfg.category_enabled("email"):
        return ""
    if not _container_running(cfg):
        return ""
    dkim_dir = "/opt/homelab/data/dms/config/rspamd/dkim"
    primary = f"rsa-2048-{DEFAULT_DKIM_SELECTOR}-{domain}.public.dns.txt"
    cmd = (
        f"dir={shlex.quote(dkim_dir)}; "
        f"txt=$(ls $dir/{shlex.quote(primary)} 2>/dev/null "
        f"|| ls $dir/*{shlex.quote(domain)}*.public*.txt 2>/dev/null | head -1); "
        'if [ -n "$txt" ] && [ -s "$txt" ]; then cat "$txt"; fi'
    )
    try:
        rc, out, _ = _mail_ssh_exec(cfg, cmd, timeout=15)
    except Exception:
        return ""
    return _parse_dkim_txt(out or "")


_DKIM_PUBKEY_RE = re.compile(r"(p=[A-Za-z0-9+/=]+)")


def _parse_dkim_txt(raw: str) -> str:
    """Extract the ``v=DKIM1; k=rsa; p=<key>`` value from an OpenDKIM TXT file."""
    if not raw:
        return ""
    text = " ".join(line.strip() for line in raw.splitlines() if line.strip())
    # OpenDKIM wraps the record in quotes + parentheses; strip them.
    text = text.replace("(", " ").replace(")", " ").replace('"', " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Require v=DKIM1 (some older DKIM emitters skipped it; Rspamd always includes).
    if "v=DKIM1" not in text and "v=DKIM1;" not in text.replace(";", " ").upper():
        # Maybe just the p= part was emitted (older DMS variants).
        m = _DKIM_PUBKEY_RE.search(text)
        if m:
            return f"v=DKIM1; k=rsa; {m.group(1)};"
    return text


def _imap_login_works(cfg: Config, email: str, password: str, *, root=None) -> tuple[bool, str]:
    """Live IMAP bind-as-user login test against Dovecot on the mail VM (infra).

    Validates the same TLS and LDAP authentication path used by mail clients.
    Returns (ok, detail). Runs python3 inside the mailserver container.
    """
    script = (
        "import imaplib, os; "
        "user = os.environ['HOMELAB_IMAP_USER']; "
        "pw = os.environ['HOMELAB_IMAP_PASSWORD']; "
        "hostname = os.environ['HOMELAB_MAIL_HOSTNAME']; "
        "m = imaplib.IMAP4('127.0.0.1', 143)\n"
        "import ssl as _ssl; ctx=_ssl.create_default_context(); "
        "m.host=hostname; m.starttls(ssl_context=ctx)\n"
        "typ, _ = m.login(user, pw)\n"
        "m.logout()\n"
        "print('OK', typ)\n"
    )
    try:
        from toolkit.core.manifest.placement import service_address
        from toolkit.services.sdk import docker_exec_on_vm

        rc, out = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["python3", "-c", script],
            service_address(cfg, "mailserver"),
            root or Path("/opt/homelab"),
            timeout=25,
            secret_environment={
                "HOMELAB_IMAP_USER": email,
                "HOMELAB_IMAP_PASSWORD": password,
                "HOMELAB_MAIL_HOSTNAME": f"mail.{cfg.domain}",
            },
        )
        err = ""
    except Exception as exc:
        return False, f"IMAP test unreachable ({str(exc)[:60]})"
    combined = (out or "") + (err or "")
    text = combined.strip().splitlines()[-1] if combined.strip() else ""
    if " OK" in text.upper():
        return True, "Dovecot STARTTLS certificate + LLDAP login ok"
    return False, f"IMAP login failed ({text[:80]})"


def _ensure_data_dirs(cfg: Config) -> list[str]:
    """Create DMS data dirs + chown so the ``docker`` user in the container can write."""
    if not cfg.is_multi_node or not cfg.category_enabled("email"):
        return []
    cmd = (
        "mkdir -p /opt/homelab/data/dms/mail /opt/homelab/data/dms/state "
        "/opt/homelab/data/dms/log /opt/homelab/data/dms/config "
        "/opt/homelab/data/roundcube && "
        # Maildirs are uid 5000 (DMS docker user); Roundcube runs as www-data.
        # Do NOT touch state/ — the postfix spool under it has DMS-managed mixed
        # ownership and a blanket chown breaks private/rewrite sockets.
        "chown -R 5000:5000 /opt/homelab/data/dms/mail && "
        "chown -R 33:33 /opt/homelab/data/roundcube"
    )
    try:
        rc, out, err = _mail_ssh_exec(cfg, cmd, timeout=20)
    except Exception as exc:
        return [f"DMS: data dir setup skipped ({str(exc)[:60]})"]
    if rc != 0:
        return [f"DMS: data dir setup failed ({(err or out)[:80]})"]
    return []


def repair_dms_state_permissions(cfg: Config, *, state_path: str = "") -> list[str]:
    """Repair the mixed ownership contract required by Docker-Mailserver.

    This runs on the guest hosting Docker, outside the container namespace.
    That distinction matters for bind mounts backed by an unprivileged LXC:
    the container can chown files but cannot chmod the ZFS mount, which leaves
    Postfix unable to traverse its private/public queue directories.
    """
    path = (state_path or "/opt/homelab/data/dms/state").strip()
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or path == "/" or any(part in {"", ".", ".."} for part in parsed.parts):
        return [f"DMS: state permission repair skipped (unsafe path {path!r})"]

    # Discover numeric IDs from the exact image in use.  DMS documents these
    # as image-owned values and they may change between image releases.
    rc, ids, err = _mail_ssh_exec(
        cfg,
        "docker exec mailserver sh -c "
        '\'printf "%s %s %s" "$(id -u postfix)" "$(id -g postfix)" '
        '"$(getent group postdrop | cut -d: -f3)"\'',
        timeout=15,
    )
    values = (ids or "").strip().split()
    if rc != 0 or len(values) != 3 or not all(value.isdigit() for value in values):
        return [f"DMS: state permission repair skipped ({(err or ids or 'could not read image ids')[:80]})"]
    postfix_uid, postfix_gid, postdrop_gid = values
    quoted = shlex.quote(path)
    script = f"""
set -eu
state={quoted}
test -d "$state" || exit 0
changed=0
check() {{
  expected=$1; actual=$(stat -c '%u:%g:%a' "$2" 2>/dev/null || printf missing)
  [ "$actual" = "$expected" ] || changed=1
}}
check '{postfix_uid}:{postfix_gid}:700' "$state/lib-postfix"
check '0:0:755' "$state/spool-postfix"
check '{postfix_uid}:0:700' "$state/spool-postfix/active"
check '{postfix_uid}:{postfix_gid}:700' "$state/spool-postfix/private"
check '{postfix_uid}:{postdrop_gid}:730' "$state/spool-postfix/maildrop"
check '{postfix_uid}:{postdrop_gid}:710' "$state/spool-postfix/public"
if [ "$changed" -eq 1 ]; then
  chmod +x "$state"
  chown -R {postfix_uid}:0 "$state/spool-postfix"
  chown 0:0 "$state/spool-postfix"
  chown -R {postfix_uid}:{postfix_gid} "$state/lib-postfix"
  chgrp -R {postdrop_gid} "$state/spool-postfix/maildrop" "$state/spool-postfix/public"
  chown -R {postfix_uid}:{postfix_gid} "$state/spool-postfix/private"
  chmod 730 "$state/spool-postfix/maildrop"
  chmod 710 "$state/spool-postfix/public"
  chmod 700 "$state/spool-postfix/active" "$state/spool-postfix/private" "$state/lib-postfix"
  docker exec mailserver supervisorctl restart postfix >/dev/null
  printf 'DMS: repaired Postfix queue permissions and restarted postfix\\n'
else
  printf 'DMS: Postfix queue permissions already match image contract\\n'
fi
"""
    rc, out, err = _mail_ssh_exec(cfg, f"sh -c {shlex.quote(script)}", timeout=45)
    if rc != 0:
        return [f"DMS: state permission repair failed ({(err or out or 'unknown error')[:100]})"]
    return [line for line in (out or "").splitlines() if line.strip()] or ["DMS: state permission repair complete"]


def bootstrap_dms_mail(cfg: Config, secrets: dict[str, str], *, root=None) -> list[str]:
    """Post-start hook for the email category (Docker-Mailserver).

    Steps:
    1. Ensure data dirs exist + are chowned for DMS's uid (5000) + Roundcube (33).
    2. Wait for ``setup`` CLI to be ready (DMS container still booting).
    3. Generate DKIM keys if not yet present (idempotent via file-exists check).
    4. Once Dovecot is listening on 993, run a live ``IMAP4 login`` as the
       admin user via LLDAP bind auth. This validates the Dovecot
       ``DOVECOT_AUTH_BIND=yes`` path against LLDAP so we know SMTP/IMAP will
       accept user logins.
    """
    logs: list[str] = []
    if not cfg.category_enabled("email"):
        return logs
    domain = cfg.domain
    if not domain or domain == "localhost":
        logs.append("DMS: skip bootstrap (domain is localhost)")
        return logs

    logs.extend(_ensure_data_dirs(cfg))

    if not _container_running(cfg):
        logs.append("DMS: mailserver container not running — retry in next hook cycle")
        return logs

    # Wait for the setup CLI to exist (DMS entrypoint symlinks it on first start).
    for _ in range(6):
        rc, out, _ = _mail_ssh_exec(cfg, "docker exec mailserver which setup", timeout=8)
        if rc == 0 and "/setup" in (out or ""):
            break
        import time as _time

        _time.sleep(5)
    else:
        logs.append("DMS: setup CLI not ready yet — will retry")
        return logs

    logs.extend(generate_dkim_keys(cfg))

    # Apply this after the container is up so IDs come from the pinned image.
    state_path = str((root or Path("/opt/homelab")) / "data" / "dms" / "state")
    if root is not None:
        try:
            from toolkit.core.config.storage import env_path
            from toolkit.core.manifest.storage import read_role_environment

            state_path = read_role_environment(env_path(cfg.control_node, root)).get("DMS_STATE_SOURCE", state_path)
        except (OSError, ValueError):
            pass
    logs.extend(repair_dms_state_permissions(cfg, state_path=state_path))

    # IMAP login sanity check: verify Dovecot accepts LLDAP bind auth.
    # Use owner email + SSO password (the owner's actual logins).
    admin_email = (cfg.email or f"admin@{domain}").strip().lower()
    sso_pw = secrets.get("SSO_USER_PASSWORD") or secrets.get("MAIL_ADMIN_PASSWORD", "")
    # Cheap "wait for 143" check (Dovecot may need a few seconds to start).
    import time as _time

    for attempt in range(3):
        rc, _, _ = _mail_ssh_exec(cfg, "docker exec mailserver sh -c 'nc -z 127.0.0.1 143 2>&1'", timeout=8)
        if rc == 0:
            break
        if attempt == 2:
            logs.append("DMS: Dovecot IMAP (143) not accepting connections yet — defer login test")
            return logs
        _time.sleep(5)

    if not sso_pw:
        logs.append("DMS: SSO_USER_PASSWORD / MAIL_ADMIN_PASSWORD not set — skip IMAP login check")
        return logs

    ok, detail = _imap_login_works(cfg, admin_email, sso_pw)
    logs.append(f"DMS: {detail}")
    if ok:
        logs.append(f"DMS: LLDAP bind auth confirmed (dovecot accepted login as {admin_email})")
    return logs
