"""authelia service plugin.

Owns its verify() (OIDC issuer/claims/userinfo + LLDAP bind probe) on top of
the base ServicePlugin defaults (compose_service, env_vars, secrets_needed,
credentials) read from service.yaml.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import OIDCClient, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


class AutheliaPlugin(ServicePlugin):
    service = "authelia"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        import yaml
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from toolkit.core.identity.service_groups import ADMIN_SERVICE_GROUPS, build_authelia_access_rules
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.oidc import compile_oidc_clients
        from toolkit.core.manifest.variables import compile_manifest_integration_variables
        from toolkit.services.sdk.ldap import base_dn

        catalog = load_service_catalog()
        key_relative = "generated/authelia/jwks/rsa.2048.key"
        key_path = context.artifact_path(key_relative)
        if key_path.is_file() and not key_path.is_symlink():
            context.claim(key_relative)
            jwks_pem = key_path.read_text(encoding="utf-8").strip()
        else:
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            jwks_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
            context.write_text(key_relative, jwks_pem)

        hashes_relative = "generated/authelia/oidc-client-hashes.yml"
        hashes_path = context.artifact_path(hashes_relative)
        existing_hashes: dict[str, str] = {}
        if hashes_path.is_file() and not hashes_path.is_symlink():
            try:
                cached = yaml.safe_load(hashes_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    existing_hashes = {str(name): value for name, value in cached.items() if isinstance(value, str)}
            except (OSError, UnicodeError, yaml.YAMLError):
                existing_hashes = {}

        secrets = dict(context.secrets)
        clients = compile_oidc_clients(
            context.config,
            catalog,
            secrets,
            existing_hashes=existing_hashes,
        )
        integration_vars = compile_manifest_integration_variables(
            context.config,
            catalog.require(self.service),
            catalog=catalog,
        )
        from toolkit.core.ops.notifications import resolve_smtp_transport

        smtp_address = ""
        smtp_username = ""
        smtp_sender = f"Authelia <authelia@{context.config.domain}>"
        smtp_disable_require_tls = False
        smtp_password = ""
        if context.config.notifications.smtp.mode == "external":
            import ipaddress

            transport = resolve_smtp_transport(context.config, secrets)
            if transport is None:
                raise ValueError("external SMTP transport is unavailable")
            scheme = "submissions" if transport.implicit_tls else "submission"
            try:
                parsed_host = ipaddress.ip_address(transport.host)
            except ValueError:
                uri_host = transport.host
            else:
                uri_host = f"[{transport.host}]" if parsed_host.version == 6 else transport.host
            smtp_address = f"{scheme}://{uri_host}:{transport.port}"
            smtp_username = transport.username
            smtp_sender = f"Authelia <{transport.from_address}>"
            smtp_password = transport.password
        elif context.config.notifications.smtp.mode == "auto":
            managed_address = integration_vars.get("AUTHELIA_SMTP_ADDRESS", "")
            if managed_address:
                smtp_address = f"smtp://{managed_address}"
                smtp_disable_require_tls = True
        context.write_text("generated/authelia/smtp-password", smtp_password)
        context.write_text(
            "generated/authelia/notifier.env",
            ("AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE=/config/smtp-password\n" if smtp_address and smtp_password else ""),
        )
        context.write_text(
            hashes_relative,
            yaml.safe_dump({client.secret_env_var: client.secret_hash for client in clients}, sort_keys=True),
        )

        values = {
            "domain": context.config.domain,
            "lldap_url": _authelia_ldap_url(context.config),
            "lldap_base_dn": base_dn(context.config),
            "lldap_bind_password": secrets.get("LLDAP_BIND_PASSWORD", ""),
            "username_attribute": "uid",
            "mail_attribute": "mail",
            "postgres_host": "postgres",
            "postgres_port": 5432,
            "smtp_address": smtp_address,
            "smtp_username": smtp_username,
            "smtp_sender": smtp_sender,
            "smtp_disable_require_tls": smtp_disable_require_tls,
            "oidc_clients": clients,
            "oidc_hmac_secret": secrets["AUTHELIA_OIDC_HMAC_SECRET"],
            "access_rules": build_authelia_access_rules(context.config),
            "admin_group_expression": " || ".join(f'"{group}" in groups' for group in ADMIN_SERVICE_GROUPS),
            "jwks_pem": jwks_pem,
        }
        context.render_template("generated/authelia.yml", "authelia.yml.j2", values)
        context.render_template("generated/authelia/configuration.yml", "authelia.yml.j2", values)

    @property
    def oidc_client(self) -> OIDCClient | None:
        return None  # Authelia IS the OIDC provider, not a client

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.services.sdk import (
            VerifyCheck,
            authelia_oidc_discovery,
            authelia_oidc_issuer,
            container_exists_on_vm,
            docker_curl,
            ldap_bind_search_on_vm,
        )
        from toolkit.services.sdk.ldap import base_dn, bind_dn, lldap_bind_uid

        checks: list[VerifyCheck] = []
        if cfg.domain == "localhost":
            return [VerifyCheck("authelia", "skipped", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "authelia", root):
            return [VerifyCheck("authelia", "health", False, "container missing")]

        from toolkit.core.ops.notifications import probe_smtp_transport, resolve_smtp_transport

        try:
            transport = resolve_smtp_transport(cfg, secrets)
        except ValueError as exc:
            checks.append(VerifyCheck("authelia", "notifier_smtp", False, str(exc)[:120]))
        else:
            if transport is None:
                checks.append(
                    VerifyCheck(
                        "authelia",
                        "notifier_smtp",
                        True,
                        "not applicable: SMTP notifier disabled",
                    )
                )
            else:
                smtp_probe = probe_smtp_transport(transport)
                checks.append(
                    VerifyCheck(
                        "authelia",
                        "notifier_smtp",
                        smtp_probe.ok,
                        smtp_probe.detail,
                    )
                )

        expected_issuer = authelia_oidc_issuer(cfg)

        # ── api_health — process accepts HTTP (liveness, not full readiness) ───
        rc, body = docker_curl(cfg, vm_ip, "authelia", "http://localhost:9091/api/health", root=root)
        health_ok = rc == 0 and bool((body or "").strip())
        checks.append(
            VerifyCheck(
                "authelia",
                "api_health",
                health_ok,
                "HTTP 200" if health_ok else ((body or "unreachable")[:120]),
            )
        )

        # ── oidc_issuer — discovery issuer matches the public auth URL ─────────────
        data, err = authelia_oidc_discovery(cfg, vm_ip, root)
        if data is None:
            checks.append(VerifyCheck("authelia", "oidc_issuer", False, err))
        else:
            issuer = data.get("issuer", "")
            match = issuer == expected_issuer
            detail = f"issuer={issuer}" if match else f"issuer={issuer} (expected {expected_issuer})"
            checks.append(VerifyCheck("authelia", "oidc_issuer", match, detail))

        # ── oidc_claims_policy — generated config ships homelab_default claims w/ groups
        config_found = False
        for rel in ("generated/authelia.yml", "generated/authelia/configuration.yml"):
            path = root / rel
            if not path.is_file():
                continue
            config_found = True
            content = path.read_text()
            has_policy = "claims_policies:" in content and "homelab_default:" in content
            has_groups = (
                "homelab_default:" in content
                and "access_token:" in content
                and re.search(r"access_token:\s*\n(?:\s+- .+\n)*?\s+- groups", content) is not None
            )
            ok = has_policy and has_groups
            detail = (
                "homelab_default claims include groups in access_token"
                if ok
                else "missing homelab_default/groups in claims_policies"
            )
            checks.append(VerifyCheck("authelia", "oidc_claims_policy", ok, detail))
            break
        if not config_found:
            checks.append(VerifyCheck("authelia", "oidc_claims_policy", False, "authelia config not generated"))

        # ── oidc_userinfo_endpoint — discovery advertises a userinfo_endpoint ─────
        data, err = authelia_oidc_discovery(cfg, vm_ip, root)
        if data is None:
            checks.append(VerifyCheck("authelia", "oidc_userinfo_endpoint", False, err))
        else:
            userinfo = (data.get("userinfo_endpoint") or "").strip()
            has_userinfo = bool(userinfo)
            detail = userinfo if has_userinfo else "userinfo_endpoint missing from discovery document"
            checks.append(VerifyCheck("authelia", "oidc_userinfo_endpoint", has_userinfo, detail))

        # ── oidc_jwks — JWKS endpoint reachable and returns signing keys ─────────
        data, err = authelia_oidc_discovery(cfg, vm_ip, root)
        if data is None:
            checks.append(VerifyCheck("authelia", "oidc_jwks", False, err))
        else:
            jwks_uri = (data.get("jwks_uri") or "").strip()
            if not jwks_uri:
                checks.append(VerifyCheck("authelia", "oidc_jwks", False, "jwks_uri missing from discovery"))
            else:
                from urllib.parse import urlparse

                jwks_path = urlparse(jwks_uri).path or "/jwks.json"
                jwks_probe = f"http://localhost:9091{jwks_path}"
                headers = {"Host": f"auth.{cfg.domain}", "X-Forwarded-Proto": "https"}
                rc_jwks, jwks_body = docker_curl(cfg, vm_ip, "authelia", jwks_probe, root=root, headers=headers)
                jwks_ok = False
                jwks_detail = "unreachable"
                if rc_jwks == 0 and jwks_body:
                    try:
                        keys = json.loads(jwks_body).get("keys") or []
                        jwks_ok = bool(keys)
                        jwks_detail = f"{len(keys)} key(s)" if jwks_ok else "no keys in JWKS document"
                    except json.JSONDecodeError:
                        jwks_detail = "invalid JWKS JSON"
                checks.append(VerifyCheck("authelia", "oidc_jwks", jwks_ok, jwks_detail))

        # ── ldap_bind — service account can bind through LLDAP's managed client ───
        bind_password = secrets.get("LLDAP_BIND_PASSWORD", "")
        if not bind_password:
            checks.append(VerifyCheck("authelia", "ldap_bind", False, "LLDAP_BIND_PASSWORD not set"))
            return checks
        _bind_dn = bind_dn(cfg)
        _base_dn = base_dn(cfg)
        from toolkit.core.manifest.placement import service_address

        lldap_vm_ip = service_address(cfg, "lldap")
        rc, out = ldap_bind_search_on_vm(
            cfg,
            lldap_vm_ip,
            root,
            bind_password=bind_password,
            bind_dn_value=_bind_dn,
            base_dn_value=_base_dn,
            search_filter=f"(uid={lldap_bind_uid()})",
        )
        out = out or ""
        lowered = out.lower()
        detail = out.strip().splitlines()[-1] if out.strip() else ""
        if rc == 0 and ("dn:" in lowered or "# search" in lowered or "result: 0 success" in lowered):
            checks.append(VerifyCheck("authelia", "ldap_bind", True, "ldap-bind bind ok"))
        else:
            checks.append(VerifyCheck("authelia", "ldap_bind", False, (detail or f"bind failed (rc={rc})")[:120]))
        return checks

    def heal(self, cfg: Config, root: Path, *, service: str | None = None) -> list[str] | None:
        from toolkit.services.authelia.bootstrap import heal_authelia

        return heal_authelia(root)


def _authelia_ldap_url(cfg: Config) -> str:
    """Use Docker DNS when Authelia and LLDAP share a node."""
    from toolkit.core.manifest.placement import service_is_local, service_node
    from toolkit.services.sdk.ldap import ldap_url, lldap_ldap_port

    node = service_node(cfg, "authelia")
    if service_is_local(cfg, node, "lldap"):
        return f"ldap://lldap:{lldap_ldap_port()}"
    return ldap_url(cfg)
