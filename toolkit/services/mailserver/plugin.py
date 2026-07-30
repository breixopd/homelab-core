"""mailserver service plugin.

Owns its verify() (DKIM DNS, IMAP login, mail roundtrip) and post_start()
(DKIM generation + IMAP login sanity) on top of the base ServicePlugin
defaults (compose_service, env_vars, secrets_needed, credentials) read
from service.yaml.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck

_QUEUE_DEPTH_THRESHOLD = 50
_DKIM_SELECTOR = "mail"


class MailserverPlugin(ServicePlugin):
    service = "mailserver"
    category = "email"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """DKIM generation + IMAP login sanity (LLDAP bind auth)."""
        from toolkit.services.mailserver.bootstrap import bootstrap_dms_mail

        return bootstrap_dms_mail(cfg, secrets, root=root)

    def after_runtime_start(self, context, services: tuple[str, ...]) -> None:
        """Restore DMS queue permissions after every container start.

        The mail state is often a host bind mount from an unprivileged LXC.
        Docker-Mailserver cannot chmod that mount from its user namespace, so
        the owning guest must apply the mixed Postfix/Postdrop contract.
        """
        if self.service not in services:
            return
        from toolkit.core.config.config import load_config
        from toolkit.services.mailserver.bootstrap import repair_dms_state_permissions

        cfg = load_config(context.root / "config.yaml")
        for line in repair_dms_state_permissions(
            cfg,
            state_path=context.environment("DMS_STATE_SOURCE"),
        ):
            context.log(line)

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """DKIM/DMARC DNS, DMS health, IMAP login, and SMTP→IMAP roundtrip."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_exec_on_vm

        checks: list[VerifyCheck] = []
        domain = cfg.domain

        if not cfg.category_enabled("email"):
            return [
                VerifyCheck("mail", "dkim_dns", True, "email not enabled"),
                VerifyCheck("mail", "dms_health", True, "email not enabled"),
                VerifyCheck("mail", "imap_login", True, "email not enabled"),
                VerifyCheck("mail", "mail_roundtrip", True, "email not enabled"),
            ]

        if domain == "localhost":
            return [
                VerifyCheck("mail", "dkim_dns", True, "skipped (localhost)"),
                VerifyCheck("mail", "dms_health", True, "skipped (localhost)"),
                VerifyCheck("mail", "imap_login", True, "skipped (localhost)"),
                VerifyCheck("mail", "mail_roundtrip", True, "skipped (localhost)"),
            ]

        if not container_exists_on_vm(cfg, vm_ip, "mailserver", root):
            return [
                VerifyCheck("mail", "container", False, "enabled Mailserver container is missing"),
            ]

        resolvers = tuple(cfg.dns.verification_resolvers)
        checks.append(self._check_dkim_dns(domain, resolvers))
        checks.append(self._check_dmarc_dns(domain, resolvers))
        checks.extend(self._check_dms_processes(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_client_tls(cfg, domain, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_mail_queue(cfg, vm_ip, root, docker_exec_on_vm))
        checks.append(self._check_imap_login(cfg, secrets, domain, root))
        checks.append(self._check_mail_roundtrip(cfg, secrets, domain, vm_ip, root, docker_exec_on_vm))
        return checks

    def _check_dkim_dns(self, domain: str, resolvers: tuple[str, ...] = ()) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        dkim_domain = f"{_DKIM_SELECTOR}._domainkey.{domain}"
        query_errors, all_found = self._query_txt_with_dig(
            dkim_domain,
            resolvers,
            self._is_dkim_txt_record,
            "DKIM",
        )
        if all_found:
            return VerifyCheck("mail", "dkim_dns", True, f"DKIM TXT record found for {dkim_domain}")
        if resolvers:
            detail = ", ".join(query_errors) or "no TXT response"
            return VerifyCheck(
                "mail",
                "dkim_dns",
                False,
                f"DKIM DNS verification unavailable for {dkim_domain}: {detail}",
            )

        # ``host`` is a useful fallback on minimal images that omit ``dig``.
        # It must still contain a real DKIM TXT payload; matching words such as
        # ``dkim`` in an error message would be a false positive.
        try:
            proc2 = subprocess.run(
                ["host", "-t", "TXT", dkim_domain],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc2.returncode == 0:
                output = proc2.stdout + proc2.stderr
                has_dkim = self._is_dkim_txt_record(output)
                return VerifyCheck(
                    "mail",
                    "dkim_dns",
                    has_dkim,
                    f"DKIM TXT record {'found' if has_dkim else 'missing'} for {dkim_domain}",
                )
            query_errors.append(f"host exited {proc2.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            query_errors.append(type(exc).__name__)

        detail = ", ".join(query_errors) or "no TXT response"
        return VerifyCheck("mail", "dkim_dns", False, f"DKIM DNS verification unavailable for {dkim_domain}: {detail}")

    @staticmethod
    def _is_dkim_txt_record(value: str) -> bool:
        """Return true only for a TXT payload containing a DKIM public key."""
        normalized = re.sub(r"[\"\s]", "", value).lower()
        return "v=dkim1" in normalized and "p=" in normalized

    def _check_dmarc_dns(self, domain: str, resolvers: tuple[str, ...] = ()) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        dmarc_domain = f"_dmarc.{domain}"

        def is_dmarc(value: str) -> bool:
            return "v=dmarc1" in re.sub(r'["\s]', "", value).lower()

        query_errors, all_found = self._query_txt_with_dig(dmarc_domain, resolvers, is_dmarc, "DMARC")
        if all_found:
            return VerifyCheck("mail", "dmarc_dns", True, f"DMARC TXT record found for {dmarc_domain}")
        if resolvers:
            detail = ", ".join(query_errors) or "no TXT response"
            return VerifyCheck(
                "mail",
                "dmarc_dns",
                False,
                f"DMARC DNS verification unavailable for {dmarc_domain}: {detail}",
            )

        try:
            proc2 = subprocess.run(
                ["host", "-t", "TXT", dmarc_domain],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc2.returncode == 0:
                found = "v=dmarc1" in re.sub(r'["\s]', "", proc2.stdout + proc2.stderr).lower()
                return VerifyCheck(
                    "mail",
                    "dmarc_dns",
                    found,
                    f"DMARC TXT record {'found' if found else 'missing'} for {dmarc_domain}",
                )
            query_errors.append(f"host exited {proc2.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            query_errors.append(type(exc).__name__)

        detail = ", ".join(query_errors) or "no TXT response"
        return VerifyCheck(
            "mail",
            "dmarc_dns",
            False,
            f"DMARC DNS verification unavailable for {dmarc_domain}: {detail}",
        )

    @staticmethod
    def _query_txt_with_dig(
        name: str,
        resolvers: tuple[str, ...],
        predicate: Callable[[str], bool],
        label: str,
    ) -> tuple[list[str], bool]:
        """Require every configured public resolver to return the expected TXT payload."""
        query_errors: list[str] = []
        targets: tuple[str | None, ...] = resolvers if resolvers else (None,)
        for resolver in targets:
            command = ["dig", "+short", "TXT", name]
            if resolver:
                command.append(f"@{resolver}")
            suffix = f" @{resolver}" if resolver else ""
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                query_errors.append(f"dig{suffix} {type(exc).__name__}")
                continue
            if proc.returncode != 0:
                query_errors.append(f"dig{suffix} exited {proc.returncode}")
                continue
            if not predicate(proc.stdout):
                query_errors.append(f"dig{suffix} returned no {label} TXT record")
        return query_errors, not query_errors

    def _check_dms_processes(self, cfg, vm_ip, root, docker_exec_on_vm) -> list[VerifyCheck]:
        from toolkit.services.sdk import VerifyCheck

        checks: list[VerifyCheck] = []
        # DMS ships no healthcheck binary — probe the same ports as the compose
        # healthcheck (SMTP 25 + IMAP 143 listening inside the container).
        rc, out = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["sh", "-c", "nc -z 127.0.0.1 25 && nc -z 127.0.0.1 143"],
            vm_ip,
            root,
            timeout=15,
        )
        checks.append(
            VerifyCheck(
                "mail",
                "dms_health",
                rc == 0,
                "SMTP+IMAP listening" if rc == 0 else (out or "SMTP/IMAP port probe failed")[:120],
            )
        )
        rc2, out2 = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["sh", "-c", "supervisorctl status postfix dovecot 2>/dev/null || (pgrep -x postfix && pgrep -x dovecot)"],
            vm_ip,
            root,
            timeout=15,
        )
        body = (out2 or "").lower()
        postfix_ok = "postfix" in body and ("run" in body or rc2 == 0)
        dovecot_ok = "dovecot" in body and ("run" in body or rc2 == 0)
        if rc2 == 0 and not postfix_ok and not dovecot_ok:
            postfix_ok = dovecot_ok = True
        checks.append(
            VerifyCheck(
                "mail",
                "postfix",
                postfix_ok,
                "postfix running" if postfix_ok else (out2 or "postfix not running")[:120],
            )
        )
        checks.append(
            VerifyCheck(
                "mail",
                "dovecot",
                dovecot_ok,
                "dovecot running" if dovecot_ok else (out2 or "dovecot not running")[:120],
            )
        )
        return checks

    def _check_mail_queue(self, cfg, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        rc, out = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["sh", "-c", "postqueue -p 2>/dev/null | grep -cE '^[A-F0-9]' || mailq 2>/dev/null | tail -1"],
            vm_ip,
            root,
            timeout=15,
        )
        if rc != 0:
            return VerifyCheck("mail", "mail_queue", False, "mail queue depth could not be read")
        text = (out or "").strip()
        if "empty" in text.lower():
            return VerifyCheck("mail", "mail_queue", True, "mail queue empty")
        try:
            depth = int(text.splitlines()[-1].strip())
        except ValueError:
            return VerifyCheck("mail", "mail_queue", False, f"unrecognized mail queue status: {text[:80]}")
        ok = depth < _QUEUE_DEPTH_THRESHOLD
        return VerifyCheck(
            "mail",
            "mail_queue",
            ok,
            f"queue depth {depth}" + ("" if ok else f" (threshold {_QUEUE_DEPTH_THRESHOLD})"),
        )

    def _check_client_tls(self, cfg, domain, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        """Verify every public mail-client TLS mode with certificate validation."""
        from toolkit.services.sdk import VerifyCheck

        py_script = """
import os, smtplib, socket, ssl
hostname = os.environ['HOMELAB_MAIL_HOSTNAME']
ctx = ssl.create_default_context()
for port, banner in ((993, b'* OK'), (465, b'220')):
    raw = socket.create_connection(('127.0.0.1', port), timeout=15)
    with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
        tls.settimeout(15)
        if banner not in tls.recv(1024):
            raise RuntimeError(f'unexpected TLS banner on {port}')
with smtplib.SMTP('127.0.0.1', 587, timeout=15) as smtp:
    smtp._host = hostname
    smtp.ehlo('verify')
    smtp.starttls(context=ctx)
    smtp.ehlo('verify')
print('OK')
"""
        rc, out = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["python3", "-c", py_script],
            vm_ip,
            root,
            timeout=60,
            secret_environment={"HOMELAB_MAIL_HOSTNAME": f"mail.{domain}"},
        )
        ok = rc == 0 and "OK" in (out or "")
        return VerifyCheck(
            "mail",
            "client_tls",
            ok,
            "IMAPS 993, SMTPS 465, and submission STARTTLS verified"
            if ok
            else (out or "mail client TLS verification failed")[:160],
        )

    def _check_imap_login(self, cfg, secrets, domain, root) -> VerifyCheck:
        from toolkit.services.mailserver.bootstrap import _imap_login_works
        from toolkit.services.sdk import VerifyCheck

        email_addr = (cfg.email or f"admin@{domain}").strip().lower()
        password = secrets.get("SSO_USER_PASSWORD") or secrets.get("MAIL_ADMIN_PASSWORD", "")
        if not password:
            return VerifyCheck("mail", "imap_login", False, "no SSO_USER_PASSWORD or MAIL_ADMIN_PASSWORD")
        ok, detail = _imap_login_works(cfg, email_addr, password, root=root)
        return VerifyCheck("mail", "imap_login", ok, detail)

    def _check_mail_roundtrip(self, cfg, secrets, domain, vm_ip, root, docker_exec_on_vm) -> VerifyCheck:
        from toolkit.services.sdk import VerifyCheck

        email_addr = (cfg.email or f"admin@{domain}").strip().lower()
        password = secrets.get("SSO_USER_PASSWORD") or secrets.get("MAIL_ADMIN_PASSWORD", "")
        if not password:
            return VerifyCheck("mail", "mail_roundtrip", False, "SSO_USER_PASSWORD / MAIL_ADMIN_PASSWORD not set")

        marker = f"homelab-verify-{int(time.time())}@{domain}"
        py_script = """
import os, smtplib, ssl, imaplib, time
from email.message import EmailMessage
email_addr = os.environ['HOMELAB_VERIFY_EMAIL']
password = os.environ['HOMELAB_VERIFY_PASSWORD']
marker = os.environ['HOMELAB_VERIFY_MARKER']
hostname = os.environ['HOMELAB_VERIFY_HOSTNAME']
ctx = ssl.create_default_context()
msg = EmailMessage()
msg['From'] = email_addr
msg['To'] = email_addr
msg['Subject'] = marker
msg.set_content(marker)
with smtplib.SMTP('127.0.0.1', 587, timeout=30) as s:
    s._host = hostname
    s.ehlo('verify')
    s.starttls(context=ctx)
    s.ehlo('verify')
    s.login(email_addr, password)
    s.send_message(msg)
ids = []
for _ in range(4):
    time.sleep(5)
    try:
        m = imaplib.IMAP4('127.0.0.1', 143)
        m.host = hostname
        m.starttls(ssl_context=ctx)
        m.login(email_addr, password)
        m.select('INBOX')
        typ, data = m.search(None, 'SUBJECT', marker)
        ids = (data[0].split() if typ == 'OK' and data and data[0] else [])
        m.logout()
        if ids:
            break
    except Exception:
        continue
print('OK' if ids else 'MISSING')
"""
        rc, out = docker_exec_on_vm(
            cfg,
            "mailserver",
            ["python3", "-c", py_script],
            vm_ip,
            root,
            timeout=90,
            secret_environment={
                "HOMELAB_VERIFY_EMAIL": email_addr,
                "HOMELAB_VERIFY_PASSWORD": password,
                "HOMELAB_VERIFY_MARKER": marker,
                "HOMELAB_VERIFY_HOSTNAME": f"mail.{domain}",
            },
        )
        ok = rc == 0 and "OK" in (out or "")
        detail = "SMTP→IMAP delivery ok" if ok else (out or "round-trip failed")[:120]
        return VerifyCheck("mail", "mail_roundtrip", ok, detail)
