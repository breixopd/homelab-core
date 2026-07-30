"""Compile enabled service manifests into Authelia OIDC clients."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.routes import service_is_enabled


class OIDCCompilationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OIDCClientConfig:
    client_id: str
    name: str
    secret_env_var: str
    secret_hash: str
    redirect_uris: tuple[str, ...]
    public: bool
    authorization_policy: Literal["one_factor", "two_factor"]
    claims_policy: str
    scopes: tuple[str, ...]
    require_pkce: bool
    pkce_challenge_method: Literal["", "plain", "S256"]
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"]
    access_token_signed_response_alg: Literal["none"]
    userinfo_signed_response_alg: Literal["none"]
    response_types: tuple[Literal["code"], ...]
    grant_types: tuple[Literal["authorization_code", "refresh_token"], ...]


def compile_oidc_clients(
    cfg: Config,
    catalog: ServiceCatalog,
    secrets: dict[str, str],
    *,
    existing_hashes: Mapping[str, str] | None = None,
) -> tuple[OIDCClientConfig, ...]:
    """Return only enabled OIDC clients and fail before rendering on missing secrets."""
    clients: list[OIDCClientConfig] = []
    seen: set[str] = set()
    password_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)
    for manifest in catalog.manifests:
        oidc = manifest.oidc
        if oidc is None or not service_is_enabled(cfg, manifest):
            continue
        if oidc.client_id in seen:
            raise OIDCCompilationError(f"duplicate OIDC client id {oidc.client_id!r}")
        seen.add(oidc.client_id)
        secret = secrets.get(oidc.secret_env_var, "")
        if not secret:
            raise OIDCCompilationError(f"enabled OIDC client {oidc.client_id!r} requires secret {oidc.secret_env_var}")
        secret_hash = (existing_hashes or {}).get(oidc.secret_env_var, "")
        if secret_hash:
            try:
                valid_hash = password_hasher.verify(secret_hash, secret) and not password_hasher.check_needs_rehash(
                    secret_hash
                )
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                valid_hash = False
            if not valid_hash:
                secret_hash = ""
        if not secret_hash:
            secret_hash = password_hasher.hash(secret)
        clients.append(
            OIDCClientConfig(
                client_id=oidc.client_id,
                name=manifest.label,
                secret_env_var=oidc.secret_env_var,
                secret_hash=secret_hash,
                redirect_uris=tuple(uri.replace("{domain}", cfg.domain) for uri in oidc.redirect_uris),
                public=oidc.public,
                authorization_policy=oidc.authorization_policy,
                claims_policy=oidc.claims_policy or "homelab_default",
                scopes=oidc.scopes,
                require_pkce=oidc.require_pkce,
                pkce_challenge_method=oidc.pkce_challenge_method,
                token_endpoint_auth_method=oidc.token_endpoint_auth_method,
                access_token_signed_response_alg=oidc.access_token_signed_response_alg,
                userinfo_signed_response_alg=oidc.userinfo_signed_response_alg,
                response_types=oidc.response_types,
                grant_types=oidc.grant_types,
            )
        )
    return tuple(clients)
