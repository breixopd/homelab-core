"""CrowdSec service plugin — intrusion detection + IP remediation.

CrowdSec detects attacks (via scenario collections) and emits verdicts; the
Caddy bouncer (a separate forward-auth sidecar) blocks malicious IPs at the
edge. This is purely config-driven — the plugin exists so the discovery
loader registers CrowdSec and verify_hooks probes the local API + bulletin.

Pairs with Wazuh: CrowdSec = network/edge detection + remediation;
Wazuh = host-based integrity monitoring. Both feed the audit timeline.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services.sdk import VerifyCheck

_BOUNCER_STALE_SECONDS = 300  # 5 minutes
_EXPECTED_COLLECTIONS = ("crowdsecurity/caddy",)
_AGENT_TOKEN_SECRET = "CROWDSEC_AGENT_REGISTRATION_TOKEN"


def crowdsec_agent_machine_name(host_name: str) -> str:
    """Return the stable LAPI machine identifier for one configured host."""
    return f"homelab-{host_name}"


def _crowdsec_agent_ranges(cfg: Config) -> list[str]:
    """Return enrollment source ranges without requiring a future mesh address."""
    ranges: list[str] = []
    for host in cfg.external_hosts:
        if "crowdsec-agent" not in host.services:
            continue
        source = cfg.network.mesh_ipv4_cidr if host.kind == "fleet" else f"{ipaddress.ip_address(host.ip)}/32"
        if source not in ranges:
            ranges.append(source)
    return ranges


class CrowdSecPlugin(ServicePlugin):
    """Self-contained CrowdSec service definition."""

    service = "crowdsec"
    category = "security"

    def generate_artifacts(self, context) -> None:
        context.render_template(
            "generated/crowdsec/acquis.yaml",
            "acquis.yaml.j2",
            {},
        )
        ranges = _crowdsec_agent_ranges(context.config)
        token = context.secrets.get(_AGENT_TOKEN_SECRET, "")
        if ranges and len(token) < 32:
            raise RuntimeError("CrowdSec agent auto-registration requires a generated token of at least 32 characters")
        context.render_template(
            "generated/crowdsec/config.yaml.local",
            "config.yaml.local.j2",
            {
                "crowdsec_auto_registration": bool(ranges),
                "crowdsec_agent_token": token if ranges else "",
                "crowdsec_agent_ranges": ranges,
            },
        )

    def host_integration_ansible_variables(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> dict[str, str]:
        if integration != "crowdsec-agent":
            raise ValueError(f"unsupported CrowdSec host integration: {integration}")
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext

        token = load_secrets_plaintext(secrets_path(root)).get(_AGENT_TOKEN_SECRET, "")
        if len(token) < 32:
            raise RuntimeError("CrowdSec agent registration token is missing or shorter than 32 characters")
        return {
            "crowdsec_lapi_token": token,
            "crowdsec_machine_name": crowdsec_agent_machine_name(host.name),
        }

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "crowdsec-agent":
            raise ValueError(f"unsupported CrowdSec host integration: {integration}")
        from toolkit.services.sdk import systemd_unit_active

        active = systemd_unit_active(root, host, "crowdsec")
        if active is True:
            return True, "CrowdSec active"
        if active is False:
            return False, "CrowdSec inactive"
        return None, "could not query CrowdSec"

    def verify(self, cfg: Config, secrets: dict, vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify the local API responds, collections installed, and bouncer fresh."""
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            crowdsec_cscli,
            crowdsec_health_url,
            docker_curl,
        )

        if cfg.domain == "localhost" or not cfg.category_enabled("security"):
            return [VerifyCheck("crowdsec", "skipped", True, "skipped (localhost or security disabled)")]

        if not container_exists_on_vm(cfg, vm_ip, "crowdsec", root):
            return [VerifyCheck("crowdsec", "container", False, "CrowdSec container missing")]

        api_ok = False
        api_detail = ""
        rc, body = docker_curl(cfg, vm_ip, "crowdsec", crowdsec_health_url(), root=root, timeout=10)
        if rc == 0 and body:
            body_stripped = body.strip()
            try:
                payload = json.loads(body_stripped)
                api_ok = payload.get("status") == "up"
                api_detail = body_stripped[:60]
            except json.JSONDecodeError:
                api_ok = "up" in body_stripped.lower()
                api_detail = body_stripped[:60]
        else:
            api_detail = "unreachable" if not body else body[:120]

        rc, out = crowdsec_cscli(cfg, vm_ip, root, ["metrics"])
        metrics_ok = rc == 0
        metrics_detail = "ok" if metrics_ok else (out or "cscli failed")[:200]

        checks: list[VerifyCheck] = [
            VerifyCheck("crowdsec", "local-api-health", api_ok, api_detail),
            VerifyCheck("crowdsec", "cscli-metrics", metrics_ok, metrics_detail),
        ]

        rc_b, bouncers_out = crowdsec_cscli(cfg, vm_ip, root, ["bouncers", "list", "-o", "json"])
        bouncer_ok, bouncer_detail = _check_bouncers(bouncers_out if rc_b == 0 else "")
        checks.append(VerifyCheck("crowdsec", "bouncers", bouncer_ok, bouncer_detail))

        rc_c, coll_out = crowdsec_cscli(cfg, vm_ip, root, ["collections", "list"])
        coll_ok, coll_detail = _check_collections(coll_out if rc_c == 0 else "")
        checks.append(VerifyCheck("crowdsec", "collections", coll_ok, coll_detail))

        expected_agents = [
            crowdsec_agent_machine_name(host.name) for host in cfg.external_hosts if "crowdsec-agent" in host.services
        ]
        if expected_agents:
            rc_m, machine_out = crowdsec_cscli(cfg, vm_ip, root, ["machines", "list", "-o", "json"])
            checks.extend(_check_agents(machine_out if rc_m == 0 else "", expected_agents))

        return checks


def _check_agents(output: str, expected: list[str]) -> list[VerifyCheck]:
    """Require every configured agent to be validated and heartbeat recently."""
    from toolkit.services.sdk import VerifyCheck

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return [VerifyCheck("crowdsec", "agents", False, "machine listing is not valid JSON")]
    rows = payload if isinstance(payload, list) else payload.get("machines", []) if isinstance(payload, dict) else []
    by_name = {str(row["machineId"]): row for row in rows if isinstance(row, dict) and row.get("machineId")}
    missing = [name for name in expected if name not in by_name]
    if missing:
        return [VerifyCheck("crowdsec", "agents", False, f"missing machine(s): {', '.join(missing)}")]
    stale: list[str] = []
    invalid: list[str] = []
    now = datetime.now(UTC)
    for name in expected:
        row = by_name[name]
        if row.get("isValidated") is not True:
            invalid.append(name)
            continue
        heartbeat = row.get("last_heartbeat")
        try:
            at = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
            age = (now - at).total_seconds()
            if age > 900 or age < -300:
                stale.append(name)
        except (TypeError, ValueError):
            stale.append(name)
    if invalid:
        return [VerifyCheck("crowdsec", "agents", False, f"unvalidated machine(s): {', '.join(invalid)}")]
    if stale:
        return [VerifyCheck("crowdsec", "agents", False, f"stale heartbeat(s): {', '.join(stale)}")]
    return [VerifyCheck("crowdsec", "agents", True, f"{len(expected)} validated machine(s) with fresh heartbeats")]


def _check_bouncers(output: str) -> tuple[bool, str]:
    """Require the named Caddy bouncer, valid key, and recent Local API pull."""
    if not (output or "").strip():
        return False, "no bouncers registered"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False, "bouncer listing is not valid JSON"
    rows = payload if isinstance(payload, list) else payload.get("bouncers", []) if isinstance(payload, dict) else []
    # A Caddy restart or network migration can leave an older registration in
    # CrowdSec's persistent database while the new instance auto-registers
    # under its current address.  Evaluate every valid registration and use
    # the most recent pull instead of letting the first (possibly stale) row
    # mask a healthy active bouncer.
    candidates: list[tuple[datetime, dict]] = []
    saw_caddy = False
    saw_invalid_auth = False
    saw_missing_pull = False
    saw_invalid_pull = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        # CrowdSec has emitted both ``caddy@<ip>`` and separate name/address
        # fields over its releases.  Accept either representation, but never
        # accept an unqualified name or malformed address.
        name = str(row.get("name", "")).strip()
        if "@" in name:
            bouncer_name, embedded_address = name.split("@", 1)
            if bouncer_name != "caddy":
                continue
            addresses = [embedded_address]
        elif name == "caddy":
            addresses = []
        else:
            continue
        for field in ("ip", "address", "ip_address", "ipAddress"):
            value = row.get(field)
            if value is not None and str(value).strip():
                addresses.append(str(value).strip())
        if not addresses:
            continue
        try:
            normalized_addresses = {str(ipaddress.ip_address(address)) for address in addresses}
        except ValueError:
            continue
        if len(normalized_addresses) != 1:
            continue
        saw_caddy = True
        # CrowdSec has used both ``revoked`` and its inverse ``valid`` in JSON
        # output.  Require one explicit healthy signal and reject
        # contradictions for each candidate independently.
        revoked = row.get("revoked")
        valid = row.get("valid")
        if revoked is not None and revoked is not False:
            saw_invalid_auth = True
            continue
        if valid is not None and valid is not True:
            saw_invalid_auth = True
            continue
        if revoked is None and valid is None:
            saw_invalid_auth = True
            continue
        pull = row.get("last_pull") or row.get("last_api_pull")
        if not pull:
            saw_missing_pull = True
            continue
        try:
            pulled_at = datetime.fromisoformat(str(pull).replace("Z", "+00:00"))
            if pulled_at.tzinfo is None:
                pulled_at = pulled_at.replace(tzinfo=UTC)
        except ValueError:
            saw_invalid_pull = True
            continue
        candidates.append((pulled_at, row))
    if not candidates:
        if not saw_caddy:
            return False, "no bouncers registered"
        if saw_invalid_auth:
            return False, "caddy bouncer key is invalid"
        if saw_missing_pull:
            return False, "caddy bouncer has no Local API pull timestamp"
        if saw_invalid_pull:
            return False, "caddy bouncer pull timestamp is invalid"
        return False, "no bouncers registered"
    pulled_at, _caddy = max(candidates, key=lambda candidate: candidate[0])
    now = datetime.now(UTC)
    stale = (now - pulled_at).total_seconds()
    if -300 <= stale <= _BOUNCER_STALE_SECONDS:
        return True, f"caddy last pull {int(stale)}s ago"
    if stale < -300:
        return False, "caddy bouncer pull timestamp is in the future"
    return False, f"caddy stale ({int(stale)}s since last pull)"


def _check_collections(output: str) -> tuple[bool, str]:
    """Sanity-check required scenario collections are installed."""
    if not (output or "").strip():
        return False, "collections list empty"
    lowered = output.lower()
    missing = [c for c in _EXPECTED_COLLECTIONS if c.lower() not in lowered]
    if missing:
        return False, f"missing: {', '.join(missing)}"
    return True, f"required collections present ({len(_EXPECTED_COLLECTIONS)})"
