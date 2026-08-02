"""Strict declarative service-manifest contract."""

from __future__ import annotations

import math
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Exposure = Literal["public", "private"]
RotationPolicy = Literal["restart", "reconcile", "persistent"]
AuthMode = Literal["forward_auth", "oidc", "native", "split", "none"]
ProbeMethod = Literal["GET", "HEAD", "OPTIONS"]
MatchKind = Literal["exact", "prefix"]
NodeId = str
Scalar = bool | str | int | float | None
RUNTIME_SELECTORS = frozenset({"@all", "@primary", "@non-primary"})

_IDENTIFIER_PATH = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SETTING_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.[a-z][a-z0-9-]{0,62}$")
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_UPSTREAM = re.compile(r"^(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):(?P<port>[0-9]{1,5})$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_CONFIG_VARIABLE = re.compile(r"\{config\.([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\}")
_SETTING_VARIABLE = re.compile(r"\{setting\.([a-z][a-z0-9-]{0,62})\}")
_SERVICE_VARIABLE = re.compile(r"\{service\.([a-z0-9][a-z0-9-]{0,62})\.(?:address|node)\}")
_DERIVED_VARIABLE = re.compile(r"\{derived\.(?:public_url_protocol|ldap_base_dn|admin_email|edge_proxy_cidr)\}")
_PRIVATE_USE_URI_SCHEME = re.compile(r"^[a-z][a-z0-9-]{0,62}(?:\.[a-z][a-z0-9-]{0,62})+$")


class StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_absolute_path(path: str) -> str:
    segments = path.split("/")
    if (
        not path.startswith("/")
        or path == "/"
        or "//" in path
        or any(segment in {".", ".."} for segment in segments)
        or any(token in path for token in ("*", "{", "}", "?", "#", "%", "\\"))
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("route paths must be exact, absolute, and free of templates or query strings")
    return path


class RouteAuth(StrictManifestModel):
    mode: AuthMode
    passthrough_paths: tuple[str, ...] = ()
    probe_statuses: tuple[int, ...] = ()
    probe_method: ProbeMethod = "GET"

    @field_validator("passthrough_paths")
    @classmethod
    def safe_passthrough_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_validate_absolute_path(value) for value in values)
        if len(checked) != len(set(checked)):
            raise ValueError("split authentication paths must be unique")
        return checked

    @field_validator("probe_statuses")
    @classmethod
    def safe_probe_statuses(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)):
            raise ValueError("split authentication probe statuses must be unique")
        if any(status < 100 or status >= 600 for status in values):
            raise ValueError("split authentication probe statuses must be valid HTTP status codes")
        return values

    @model_validator(mode="after")
    def validate_mode(self) -> RouteAuth:
        if self.mode == "split" and not self.passthrough_paths:
            raise ValueError("split authentication requires exact passthrough paths")
        if self.mode != "split" and self.passthrough_paths:
            raise ValueError("passthrough paths are only valid for split authentication")
        if self.mode != "split" and self.probe_statuses:
            raise ValueError("probe_statuses is only valid for split authentication")
        if self.mode != "split" and self.probe_method != "GET":
            raise ValueError("probe_method is only valid for split authentication")
        return self


class ConfigPredicate(StrictManifestModel):
    path: str | None = Field(default=None, min_length=3, max_length=128)
    setting: str | None = Field(default=None, min_length=3, max_length=126)
    equals: Scalar = None
    one_of: tuple[bool | str | int | float, ...] = ()

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _IDENTIFIER_PATH.fullmatch(value) or any(part.startswith("_") for part in value.split(".")):
            raise ValueError("predicate path must contain public dotted identifiers")
        return value

    @field_validator("setting")
    @classmethod
    def safe_setting(cls, value: str | None) -> str | None:
        if value is not None and not _SETTING_REFERENCE.fullmatch(value):
            raise ValueError("predicate setting must identify a service and declared setting")
        return value

    @model_validator(mode="after")
    def exactly_one_comparison(self) -> ConfigPredicate:
        if (self.path is None) == (self.setting is None):
            raise ValueError("predicate requires exactly one of path or setting")
        has_equals = "equals" in self.model_fields_set
        has_one_of = bool(self.one_of)
        if has_equals == has_one_of:
            raise ValueError("predicate requires exactly one of equals or one_of")
        if len(self.one_of) != len(set(self.one_of)):
            raise ValueError("predicate one_of values must be unique")
        values = (self.equals,) if has_equals else self.one_of
        if any(isinstance(value, float) and not math.isfinite(value) for value in values):
            raise ValueError("predicate numeric values must be finite")
        return self


class RouteMatch(StrictManifestModel):
    kind: MatchKind
    paths: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("paths")
    @classmethod
    def safe_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_validate_absolute_path(value) for value in values)
        if len(checked) != len(set(checked)):
            raise ValueError("route match paths must be unique")
        return checked

    @model_validator(mode="after")
    def require_prefix_boundary(self) -> RouteMatch:
        if self.kind == "prefix" and any(not path.endswith("/") for path in self.paths):
            raise ValueError("prefix route paths must end with a trailing slash")
        return self


class RouteVariant(StrictManifestModel):
    when: ConfigPredicate
    upstream: str
    compose_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")

    @field_validator("upstream")
    @classmethod
    def valid_upstream(cls, value: str) -> str:
        return _validate_upstream(value)


class ResponseHeader(StrictManifestModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    value: str = Field(min_length=1, max_length=4_096)

    @field_validator("value")
    @classmethod
    def safe_value(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("response header values cannot contain control characters")
        return value


class RouteManifest(StrictManifestModel):
    subdomain: str | None = None
    upstream: str = ""
    published_port: int | None = Field(default=None, ge=1, le=65_535)
    compose_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    exposure: Exposure
    auth: RouteAuth
    match: RouteMatch | None = None
    variants: tuple[RouteVariant, ...] = ()
    file_server_root: str = Field(default="", max_length=4_096)
    response_body: str = Field(default="", max_length=32_768)
    request_body_max_mb: int | None = Field(default=None, ge=1, le=100)
    deny: tuple[RouteMatch, ...] = ()
    response_headers: tuple[ResponseHeader, ...] = ()

    @field_validator("subdomain")
    @classmethod
    def valid_subdomain(cls, value: str | None) -> str | None:
        if value is not None and value != "" and not _SUBDOMAIN.fullmatch(value):
            raise ValueError("route subdomain must be one DNS label")
        return value

    @field_validator("upstream")
    @classmethod
    def valid_upstream(cls, value: str) -> str:
        return _validate_upstream(value) if value else value

    @field_validator("file_server_root")
    @classmethod
    def valid_file_server_root(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("file server root must be absolute")
        return value

    @field_validator("response_body")
    @classmethod
    def safe_response_body(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("static response bodies cannot contain control characters")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> RouteManifest:
        if self.response_body and (self.file_server_root or self.upstream or self.variants or self.compose_service):
            raise ValueError("response routes cannot declare proxy or file-server targets")
        if self.response_body and self.auth.mode != "none":
            raise ValueError("response routes must be unauthenticated")
        if self.response_body:
            pass
        elif self.file_server_root:
            if self.upstream or self.variants or self.compose_service or self.published_port is not None:
                raise ValueError("file-server routes cannot declare proxy targets")
        elif not self.upstream and not self.variants:
            raise ValueError("proxy routes require an upstream or variant")
        if self.auth.mode == "none" and not self.response_body:
            raise ValueError("unauthenticated routes must use a static response target")
        if self.auth.mode == "split" and self.match is not None:
            raise ValueError("split authentication belongs on a default route")
        if self.deny and self.match is not None:
            raise ValueError("deny policies belong on a default route")
        denied_keys = [(match.kind, path) for match in self.deny for path in match.paths]
        if len(denied_keys) != len(set(denied_keys)):
            raise ValueError("deny route matches must be unique")
        header_names = [header.name.lower() for header in self.response_headers]
        if len(header_names) != len(set(header_names)):
            raise ValueError("response header names must be unique")
        return self


def _validate_upstream(value: str) -> str:
    match = _UPSTREAM.fullmatch(value)
    if match is None or not 1 <= int(match.group("port")) <= 65_535:
        raise ValueError("route upstream must be a host and valid port")
    host = match.group("host")
    if host != host.lower() or any(not _SUBDOMAIN.fullmatch(label) for label in host.split(".")):
        raise ValueError("route upstream host must contain valid DNS labels")
    return value


class OIDCManifest(StrictManifestModel):
    client_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    secret_env_var: str
    redirect_uris: tuple[str, ...] = Field(min_length=1, max_length=16)
    claims_policy: str = Field(default="", max_length=100)
    scopes: tuple[str, ...] = ("openid", "profile", "email", "groups")
    public: bool = False
    authorization_policy: Literal["one_factor", "two_factor"] = "one_factor"
    require_pkce: bool = False
    pkce_challenge_method: Literal["", "plain", "S256"] = ""
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic"
    access_token_signed_response_alg: Literal["none"] = "none"
    userinfo_signed_response_alg: Literal["none"] = "none"
    response_types: tuple[Literal["code"], ...] = ("code",)
    grant_types: tuple[Literal["authorization_code", "refresh_token"], ...] = ("authorization_code",)

    @field_validator("secret_env_var")
    @classmethod
    def valid_secret_name(cls, value: str) -> str:
        if not _SECRET_NAME.fullmatch(value):
            raise ValueError("OIDC secret reference must be an environment secret name")
        return value

    @field_validator("redirect_uris")
    @classmethod
    def valid_redirect_uris(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("OIDC redirect URIs must be unique")
        for value in values:
            if len(value) > 2_048 or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("OIDC redirect URI must be bounded text without control characters")
            if "{" in value.replace("{domain}", "") or "}" in value.replace("{domain}", ""):
                raise ValueError("OIDC redirect URI supports only the {domain} template")
            try:
                parsed = urlsplit(value)
                parsed.port
            except ValueError as exc:
                raise ValueError("OIDC redirect URI is malformed") from exc
            is_https = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
            )
            is_native_app = (
                bool(_PRIVATE_USE_URI_SCHEME.fullmatch(parsed.scheme))
                and not parsed.netloc
                and parsed.path.startswith("/")
            )
            if not (is_https or is_native_app) or parsed.query or parsed.fragment:
                raise ValueError("OIDC redirect URI must use HTTPS or a reverse-domain native-app scheme")
        return values

    @field_validator("claims_policy")
    @classmethod
    def valid_claims_policy(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", value):
            raise ValueError("OIDC claims policy must be a safe identifier")
        return value

    @model_validator(mode="after")
    def validate_protocol_capabilities(self) -> OIDCManifest:
        if self.public:
            raise ValueError("public OIDC clients are not supported by managed service manifests")
        if self.require_pkce != bool(self.pkce_challenge_method):
            raise ValueError("OIDC PKCE requirement and challenge method must be configured together")
        for name, values in (
            ("scope", self.scopes),
            ("response type", self.response_types),
            ("grant type", self.grant_types),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"OIDC {name} values must be unique")
        if "openid" not in self.scopes:
            raise ValueError("OIDC scopes must include openid")
        has_refresh = "refresh_token" in self.grant_types
        if ("offline_access" in self.scopes) != has_refresh:
            raise ValueError("OIDC offline_access scope and refresh_token grant must be configured together")
        return self


class DataManifest(StrictManifestModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    source_env: str | None = None
    source_subpath: str = ""
    volume: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    runtime_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    target: str = Field(min_length=1, max_length=4_096)
    size_estimate_gb: int = Field(ge=0, le=1_000_000)
    snapshot: bool = True
    manage_permissions: bool = True
    shared: bool = False
    host_uid: int = Field(default=0, ge=0, le=65_535)
    host_gid: int = Field(default=0, ge=0, le=65_535)
    host_subdirs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("source_env")
    @classmethod
    def valid_source_env(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_NAME.fullmatch(value):
            raise ValueError("data source environment variable is invalid")
        return value

    @field_validator("source_subpath")
    @classmethod
    def safe_source_subpath(cls, value: str) -> str:
        if value and (value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("data source subpath must be a normalized relative path")
        return value

    @field_validator("host_subdirs")
    @classmethod
    def safe_host_subdirs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("data host subdirectories must be unique")
        for value in values:
            if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
                raise ValueError("data host subdirectories must be normalized relative paths")
        return values

    @field_validator("target")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value == "/"
            or "//" in value
            or any(segment in {".", ".."} for segment in value.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("data target must be a safe absolute container path")
        return value

    @model_validator(mode="after")
    def exactly_one_source(self) -> DataManifest:
        if (self.source_env is None) == (self.volume is None):
            raise ValueError("data asset requires exactly one source_env or volume")
        if self.source_subpath and self.source_env is None:
            raise ValueError("data source subpath requires source_env")
        if self.shared and (self.snapshot or self.manage_permissions or self.size_estimate_gb != 0):
            raise ValueError(
                "shared data must disable snapshots and permission management and estimate zero owned storage"
            )
        return self


class BackupExportManifest(StrictManifestModel):
    artifact: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,126}\.gz$")
    strategy: Literal["container", "sqlite"]
    command: tuple[str, ...] = Field(default=(), max_length=32)
    runtime_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    container: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9_.-]{0,127})?$")
    data_spec: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    database_path: str = Field(default="", max_length=4_096)
    timeout_seconds: int = Field(default=900, ge=30, le=3_600)

    @field_validator("command")
    @classmethod
    def safe_command(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value
            or len(value) > 16_384
            or "\x00" in value
            or any(ord(character) < 32 and character not in {"\n", "\t"} for character in value)
            for value in values
        ):
            raise ValueError("backup export command arguments must be bounded text without unsafe controls")
        return values

    @field_validator("database_path")
    @classmethod
    def safe_database_path(cls, value: str) -> str:
        if value and (
            value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("backup database path must be a normalized relative path")
        return value

    @model_validator(mode="after")
    def complete_strategy(self) -> BackupExportManifest:
        if self.strategy == "container":
            if not self.command or self.data_spec or self.database_path:
                raise ValueError("container backup exports require only a command")
        elif self.command or self.runtime_service or self.container or not self.data_spec or not self.database_path:
            raise ValueError("SQLite backup exports require only a data spec and database path")
        return self


class HostPathManifest(StrictManifestModel):
    path: str = Field(min_length=1, max_length=512)
    uid: int = Field(default=0, ge=0, le=65_535)
    gid: int = Field(default=0, ge=0, le=65_535)
    mode: str = Field(default="0755", pattern=r"^[0-7]{4}$")
    subdirs: tuple[str, ...] = Field(default=(), max_length=16)
    create: bool = False
    recursive: bool = True

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("host path must be a normalized relative path")
        return value

    @field_validator("subdirs")
    @classmethod
    def safe_subdirs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("host path subdirectories must be unique")
        for value in values:
            if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
                raise ValueError("host path subdirectories must be normalized relative paths")
        return values


class SecretSetupManifest(StrictManifestModel):
    label: str = Field(min_length=1, max_length=100)
    input: Literal["password", "text"] = "password"
    required: bool = False
    when: tuple[ConfigPredicate, ...] = Field(default=(), max_length=8)


class RequiredSecretManifest(StrictManifestModel):
    name: str
    tier: Literal["user", "generated", "bootstrapped", "derived"]
    description: str = Field(min_length=1, max_length=500)
    length: int = Field(default=32, ge=16, le=4_096)
    default: str | None = Field(default=None, min_length=1, max_length=256)
    fallback_env: str | None = None
    generator: Literal["token", "password"] = "token"
    setup: SecretSetupManifest | None = None
    # Generated secrets must opt into an explicit lifecycle.  ``persistent``
    # means the value is bound to durable state and cannot be safely replaced
    # by the generic rotation command.
    rotation: RotationPolicy = "persistent"

    @field_validator("name", "fallback_env")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_NAME.fullmatch(value):
            raise ValueError("secret name is invalid")
        return value

    @model_validator(mode="after")
    def distinct_fallback(self) -> RequiredSecretManifest:
        if self.fallback_env == self.name:
            raise ValueError("secret fallback source must differ from its target")
        if self.default is not None and self.tier != "generated":
            raise ValueError("fixed defaults require the generated tier")
        if self.tier == "generated" and "rotation" not in self.model_fields_set:
            raise ValueError("generated secrets must explicitly declare a rotation policy")
        if self.default is not None and self.rotation != "persistent":
            raise ValueError("secrets with fixed defaults must use persistent rotation")
        if self.generator == "password" and self.tier != "generated":
            raise ValueError("password generators require the generated tier")
        return self


class SecretProjectionManifest(StrictManifestModel):
    """Map one stored secret to a service-specific runtime environment name."""

    source_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    target_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")

    @model_validator(mode="after")
    def distinct_names(self) -> SecretProjectionManifest:
        if self.source_env == self.target_env:
            raise ValueError("secret projection source and target must differ")
        return self


class CredentialManifest(StrictManifestModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2_048)
    username_env: str | None = None
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password_env: str
    tags: tuple[str, ...] = ()
    notes: str = Field(default="", max_length=1_000)

    @field_validator("username_env", "password_env")
    @classmethod
    def valid_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_NAME.fullmatch(value):
            raise ValueError("credential environment reference is invalid")
        return value


class HealthManifest(StrictManifestModel):
    public_probe_path: str = ""
    starting_policy: Literal["fail", "pending"] = "fail"

    @field_validator("public_probe_path")
    @classmethod
    def valid_public_probe_path(cls, value: str) -> str:
        return value if value in {"", "/"} else _validate_absolute_path(value)


class OperatorGuidanceManifest(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    phase: Literal["pre_deploy", "post_deploy"]
    category: Literal["Prerequisite", "Required", "Verify", "Optional"]
    title: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=2_000)
    route_url: bool = False

    @field_validator("title", "instructions")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in {"\n", "\t"} for character in value):
            raise ValueError("operator guidance cannot contain control characters")
        return value

    @field_validator("instructions")
    @classmethod
    def safe_templates(cls, value: str) -> str:
        remainder = value.replace("{domain}", "").replace("{url}", "")
        if "{" in remainder or "}" in remainder:
            raise ValueError("operator guidance supports only {domain} and {url} templates")
        return value

    @model_validator(mode="after")
    def valid_phase(self) -> OperatorGuidanceManifest:
        if self.phase == "pre_deploy" and self.category != "Prerequisite":
            raise ValueError("pre-deploy guidance must be a prerequisite")
        if self.phase == "post_deploy" and self.category == "Prerequisite":
            raise ValueError("post-deploy guidance cannot be a prerequisite")
        if "{url}" in self.instructions and not self.route_url:
            raise ValueError("operator guidance using {url} must enable route_url")
        return self


class OperatorBookmarkManifest(StrictManifestModel):
    section: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=100, ge=0, le=10_000)
    description: str = Field(min_length=1, max_length=240)
    route_subdomain: str | None = None

    @field_validator("route_subdomain")
    @classmethod
    def valid_route_subdomain(cls, value: str | None) -> str | None:
        if value is not None and value != "" and not _SUBDOMAIN.fullmatch(value):
            raise ValueError("operator bookmark route_subdomain must be one DNS label")
        return value


class InviteCardManifest(StrictManifestModel):
    group: str = Field(pattern=r"^homelab-[a-z0-9][a-z0-9-]{0,54}$")
    priority: int = Field(default=100, ge=0, le=10_000)
    path: str = "/"
    blurb: str = Field(min_length=1, max_length=240)
    sign_in: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or any(token in value for token in ("{", "}", "\\"))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("invite card paths must be safe route-relative URLs")
        return value


class IdentityProvisioningManifest(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    mode: Literal["first_login", "plugin"]
    priority: int = Field(default=100, ge=0, le=10_000)
    message: str = Field(default="", max_length=500)
    disabled_message: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def first_login_has_message(self) -> IdentityProvisioningManifest:
        if self.mode == "first_login" and not self.message:
            raise ValueError("first-login provisioning requires a message")
        return self


class ServiceIdentityManifest(StrictManifestModel):
    access_groups: tuple[str, ...] = Field(default=(), max_length=16)
    invite: InviteCardManifest | None = None
    provisioning: tuple[IdentityProvisioningManifest, ...] = Field(default=(), max_length=16)

    @field_validator("access_groups")
    @classmethod
    def valid_access_groups(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("service access groups must be unique")
        if any(not re.fullmatch(r"homelab-[a-z0-9][a-z0-9-]{0,54}", value) for value in values):
            raise ValueError("service access groups must be homelab group names")
        return values

    @model_validator(mode="after")
    def invite_uses_explicit_group(self) -> ServiceIdentityManifest:
        if self.invite is not None and self.access_groups and self.invite.group not in self.access_groups:
            raise ValueError("invite group must be included in explicit service access groups")
        ids = [entry.id for entry in self.provisioning]
        if len(ids) != len(set(ids)):
            raise ValueError("identity provisioning IDs must be unique within a service")
        return self


class PrometheusScrapeManifest(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    job: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    container_port: int | None = Field(default=None, ge=1, le=65_535)
    host_port: int | None = Field(default=None, ge=1, le=65_535)
    runtime_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    host_integration: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    path: str = "/metrics"

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _validate_absolute_path(value)

    @model_validator(mode="after")
    def valid_target(self) -> PrometheusScrapeManifest:
        if self.host_integration:
            if self.runtime_service or self.container_port is not None or self.host_port is None:
                raise ValueError("host-integration scrapes require only host_port")
        elif self.container_port is None:
            raise ValueError("container and runtime scrapes require container_port")
        return self


class DatabaseProviderManifest(StrictManifestModel):
    """Database runtime and administrator contract exposed by a service."""

    engine: Literal["postgresql"]
    admin_username_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    admin_password_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    admin_database_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class DatabaseBindingManifest(StrictManifestModel):
    """One service-owned database and role requested from a provider."""

    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    database: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    username: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    host_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    port_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    database_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    username_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    password_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class ServiceEndpointManifest(StrictManifestModel):
    """Primary TCP endpoint exposed to other service plugins."""

    compose_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    container_port: int = Field(ge=1, le=65_535)
    published_port: int | None = Field(default=None, ge=1, le=65_535)


class ServiceIntegrationManifest(StrictManifestModel):
    """Environment outputs used by one plugin to reach another plugin."""

    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    required: bool = True
    enabled_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    host_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    port_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    address_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    scheme: Literal["http", "https", "ldap", "ldaps", "redis", "rediss", "smtp", "smtps"] | None = None

    @model_validator(mode="after")
    def valid_outputs(self) -> ServiceIntegrationManifest:
        outputs = tuple(
            value for value in (self.enabled_env, self.host_env, self.port_env, self.address_env, self.url_env) if value
        )
        if not outputs:
            raise ValueError("service integration requires at least one environment output")
        if len(outputs) != len(set(outputs)):
            raise ValueError("service integration environment outputs must be unique")
        if self.required and (self.host_env is None or self.port_env is None):
            raise ValueError("required service integration requires host and port environment outputs")
        if (self.url_env is None) != (self.scheme is None):
            raise ValueError("service integration URL output and scheme must be declared together")
        return self


class NetworkListenerManifest(StrictManifestModel):
    """A host listener intentionally reachable outside its local Compose network."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    port: int = Field(ge=1, le=65_535)
    protocol: Literal["tcp", "udp"] = "tcp"
    runtime_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")
    host_process: bool = False
    sources: tuple[str, ...] = Field(min_length=1, max_length=32)
    enabled_when: tuple[ConfigPredicate, ...] = ()

    @field_validator("sources")
    @classmethod
    def valid_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        from toolkit.core.machines.models import validate_machine_id

        if len(values) != len(set(values)):
            raise ValueError("network listener sources must be unique")
        for value in values:
            if value in {"@all", "@internet", "@lan", "@mesh"}:
                continue
            if value.startswith(("@runtime:", "@service:", "@integration:")):
                _, _, reference = value.partition(":")
                validate_machine_id(reference)
                continue
            validate_machine_id(value)
        return values

    @model_validator(mode="after")
    def valid_target(self) -> NetworkListenerManifest:
        if self.host_process and self.runtime_service:
            raise ValueError("host-process listeners cannot declare a Compose runtime")
        return self


class ServiceSetting(StrictManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    type: Literal["boolean", "number", "text", "select"]
    default: bool | int | float | str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    choices: tuple[str, ...] = Field(default=(), max_length=32)
    requires_redeploy: bool = True
    setup: bool = False

    @model_validator(mode="after")
    def validate_type_constraints(self) -> ServiceSetting:
        if self.type == "select":
            if len(self.choices) < 2 or len(self.choices) != len(set(self.choices)):
                raise ValueError("select settings require at least two unique choices")
        elif self.choices:
            raise ValueError("choices are only valid for select settings")
        if self.type != "number" and any(value is not None for value in (self.minimum, self.maximum, self.step)):
            raise ValueError("minimum, maximum, and step are only valid for number settings")
        if self.minimum is not None and self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("maximum must be greater than or equal to minimum")
        if self.type == "boolean" and not isinstance(self.default, bool):
            raise ValueError("boolean setting default must be a boolean")
        if self.type == "number":
            if isinstance(self.default, bool) or not isinstance(self.default, int | float):
                raise ValueError("number setting default must be numeric")
            if isinstance(self.default, float) and not math.isfinite(self.default):
                raise ValueError("number setting default must be finite")
            if self.minimum is not None and self.default < self.minimum:
                raise ValueError("setting default is below minimum")
            if self.maximum is not None and self.default > self.maximum:
                raise ValueError("setting default is above maximum")
        if self.type in {"text", "select"} and not isinstance(self.default, str):
            raise ValueError("text setting default must be text")
        if self.type == "select" and self.default not in self.choices:
            raise ValueError("select setting default must be an allowed choice")
        return self


class ServiceAction(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    confirmation: str = Field(default="", max_length=200)
    is_dangerous: bool = False


class ServiceInfoItem(StrictManifestModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)
    copyable: bool = False
    href: str = Field(default="", max_length=2_048)

    @field_validator("value", "href")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if "\x00" in value or any(ord(character) < 32 for character in value):
            raise ValueError("service panel values must not contain control characters")
        if value.lower().strip().startswith(("javascript:", "data:")):
            raise ValueError("service panel links must use a safe scheme")
        return value


class ServiceInfoPanel(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    items: tuple[ServiceInfoItem, ...] = Field(min_length=1, max_length=16)


class ServiceSecretField(StrictManifestModel):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class ServiceMetric(StrictManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    source: Literal["status", "prometheus"]
    field: str = Field(default="", pattern=r"^[a-z][a-z0-9_.]{0,127}$")
    query: str = Field(default="", max_length=2_000, pattern=r"^[^\x00-\x1f\x7f]*$")
    unit: Literal["none", "count", "percent", "bytes", "megabytes", "seconds", "mbps"] = "none"
    precision: int = Field(default=0, ge=0, le=4)

    @model_validator(mode="after")
    def validate_source(self) -> ServiceMetric:
        if self.source == "status" and (not self.field or self.query):
            raise ValueError("status metrics require field and forbid query")
        if self.source == "prometheus" and (not self.query or self.field):
            raise ValueError("prometheus metrics require query and forbid field")
        return self


class ServiceResourceColumn(StrictManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)


class ServiceResource(StrictManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    columns: tuple[ServiceResourceColumn, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_columns(self) -> ServiceResource:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate service resource column identifier")
        return self


class ServiceManagement(StrictManifestModel):
    panels: tuple[ServiceInfoPanel, ...] = Field(default=(), max_length=8)
    secrets: tuple[ServiceSecretField, ...] = Field(default=(), max_length=32)
    settings: tuple[ServiceSetting, ...] = Field(default=(), max_length=32)
    actions: tuple[ServiceAction, ...] = Field(default=(), max_length=16)
    metrics: tuple[ServiceMetric, ...] = Field(default=(), max_length=32)
    resources: tuple[ServiceResource, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def unique_capability_ids(self) -> ServiceManagement:
        for label, values in (
            ("panel", [item.id for item in self.panels]),
            ("secret", [item.name for item in self.secrets]),
            ("setting", [item.key for item in self.settings]),
            ("action", [item.id for item in self.actions]),
            ("metric", [item.key for item in self.metrics]),
            ("resource", [item.key for item in self.resources]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate management {label} identifier")
        if any(metric.key.startswith("container_") for metric in self.metrics):
            raise ValueError("container_ metric identifiers are reserved for built-in telemetry")
        if sum(metric.source == "prometheus" for metric in self.metrics) > 12:
            raise ValueError("a service can declare at most 12 Prometheus metrics")
        return self


class HostIntegrationFieldManifest(StrictManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    type: Literal["boolean", "integer", "number", "path", "text"] = "text"
    required: bool = False
    default: Scalar = None
    placeholder: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_default(self) -> HostIntegrationFieldManifest:
        value = self.default
        if value is None:
            return self
        valid = {
            "boolean": type(value) is bool,
            "integer": type(value) is int,
            "number": type(value) in {int, float},
            "path": isinstance(value, str),
            "text": isinstance(value, str),
        }[self.type]
        if not valid:
            raise ValueError(f"host integration field {self.key!r} has an invalid {self.type} default")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("host integration field numeric defaults must be finite")
        if isinstance(value, str) and len(value) > 4_096:
            raise ValueError("host integration field defaults cannot exceed 4096 characters")
        if isinstance(value, str) and any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("host integration field defaults cannot contain control characters")
        if self.type == "path" and isinstance(value, str):
            _validate_host_integration_path(value)
        return self


def _validate_host_integration_path(value: str) -> str:
    segments = value.split("/")
    if (
        not value.startswith("/")
        or value == "/"
        or "//" in value
        or any(segment in {".", ".."} for segment in segments)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("host integration paths must be an absolute path without traversal or control characters")
    return value


class HostIntegrationManifest(StrictManifestModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    kinds: tuple[Literal["plain", "fleet"], ...] = Field(min_length=1, max_length=2)
    default_for: tuple[Literal["plain", "fleet"], ...] = Field(default=(), max_length=2)
    after: tuple[str, ...] = Field(default=(), max_length=16)
    ansible_role: str = Field(default="", pattern=r"^(?:[a-z][a-z0-9_]{0,62})?$")
    managed_node_bootstrap: bool = False
    controller_lifecycle: bool = False
    fields: tuple[HostIntegrationFieldManifest, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_host_integration(self) -> HostIntegrationManifest:
        if len(self.kinds) != len(set(self.kinds)) or len(self.default_for) != len(set(self.default_for)):
            raise ValueError("host integration kinds must be unique")
        if not set(self.default_for).issubset(self.kinds):
            raise ValueError("host integration defaults must be selectable for that host kind")
        if len(self.after) != len(set(self.after)) or self.id in self.after:
            raise ValueError("host integration ordering dependencies must be unique and cannot reference itself")
        if self.managed_node_bootstrap and not self.ansible_role:
            raise ValueError("managed-node bootstrap integrations require an ansible_role")
        field_keys = [field.key for field in self.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("host integration field identifiers must be unique")
        return self


class RuntimeServiceManifest(StrictManifestModel):
    """Node placement and host requirements for one Compose runtime."""

    placements: tuple[str, ...] = Field(default=(), max_length=128)
    compose_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    mode: Literal["daemon", "oneshot"] = "daemon"
    memory_floor_mb: int | None = Field(default=None, ge=64, le=65_536)
    cpu_floor: float | None = Field(default=None, ge=0.05, le=64)
    required_host_paths: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("placements")
    @classmethod
    def valid_placements(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        from toolkit.core.machines.models import validate_machine_id

        if len(values) != len(set(values)):
            raise ValueError("runtime placements must be unique")
        for selector in values:
            if selector not in RUNTIME_SELECTORS:
                validate_machine_id(selector)
        return values

    @field_validator("required_host_paths")
    @classmethod
    def safe_required_host_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("runtime host paths must be unique")
        for value in values:
            if (
                not value.startswith("/")
                or value == "/"
                or "//" in value
                or any(segment in {".", ".."} for segment in value.split("/"))
                or any(token in value for token in ("*", "{", "}", "?", "#", "%", "\\"))
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("runtime host paths must be safe absolute paths")
        return values


def _validate_relative_host_source_path(value: str) -> str:
    if (
        not value
        or value == "config"
        or value.startswith("/")
        or "//" in value
        or "\\" in value
        or re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("host source paths must be normalized relative paths")
    return value


class HostSourceVariantManifest(StrictManifestModel):
    when: ConfigPredicate
    path: str

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _validate_relative_host_source_path(value)


class HostSourceManifest(StrictManifestModel):
    path: str
    variants: tuple[HostSourceVariantManifest, ...] = Field(default=(), max_length=16)
    static: bool = False

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _validate_relative_host_source_path(value)

    @model_validator(mode="after")
    def unique_variants(self) -> HostSourceManifest:
        predicates = [variant.when.model_dump_json() for variant in self.variants]
        if len(predicates) != len(set(predicates)):
            raise ValueError("host source variants require unique predicates")
        paths = (self.path, *(variant.path for variant in self.variants))
        if self.static != all(path.startswith("config/") for path in paths):
            raise ValueError("static host sources must use config/ and config/ host sources must be static")
        return self


class GeneratedArtifactManifest(StrictManifestModel):
    path: str
    kind: Literal["file", "symlink"] = "file"
    sensitive: bool = False
    executable: bool = False
    # Most generated files can use the safe defaults derived from their
    # sensitivity/executable flags.  A plugin may override mode and host
    # ownership when the runtime is rootless and the host's root mapping is
    # not readable from the container user namespace.
    mode: str | None = Field(default=None, pattern=r"^[0-7]{4}$")
    host_uid: int | None = Field(default=None, ge=0, le=65_535)
    host_gid: int | None = Field(default=None, ge=0, le=65_535)
    runtime_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if (
            not value
            or not value.startswith("generated/")
            or value.startswith("/")
            or "//" in value
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("generated artifact paths must be normalized relative paths below generated/")
        return value

    @model_validator(mode="after")
    def valid_kind(self) -> GeneratedArtifactManifest:
        owner_declared = self.host_uid is not None or self.host_gid is not None
        if owner_declared and (self.host_uid is None or self.host_gid is None):
            raise ValueError("generated artifact ownership requires both host_uid and host_gid")
        if self.kind == "symlink" and (self.sensitive or self.executable or self.mode or owner_declared):
            raise ValueError("generated artifact symlink cannot declare file metadata")
        return self


def _validate_relative_build_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "//" in value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("image build paths must be normalized relative paths")
    return value


class ImageSmokeTestManifest(StrictManifestModel):
    entrypoint: str = Field(default="", max_length=1_024)
    command: tuple[str, ...] = Field(min_length=1, max_length=32)
    contains: str = Field(default="", max_length=4_096)

    @field_validator("entrypoint", "contains")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if "\x00" in value or any(ord(character) < 32 and character not in {"\n", "\t"} for character in value):
            raise ValueError("image smoke values must be bounded text without unsafe controls")
        return value

    @field_validator("command")
    @classmethod
    def safe_command(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value
            or len(value) > 16_384
            or "\x00" in value
            or any(ord(character) < 32 and character not in {"\n", "\t"} for character in value)
            for value in values
        ):
            raise ValueError("image smoke command arguments must be bounded text without unsafe controls")
        return values


class ImageBuildManifest(StrictManifestModel):
    context: str
    env_var: str
    repository: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9._-]{0,127})?$")
    dockerfile: str = "Dockerfile"
    repository_context: bool = False
    ci: bool = True
    platforms: tuple[Literal["linux/amd64", "linux/arm64"], ...] = ("linux/amd64", "linux/arm64")
    smoke_tests: tuple[ImageSmokeTestManifest, ...] = Field(default=(), max_length=8)
    requirements: str = ""

    @field_validator("context")
    @classmethod
    def safe_context(cls, value: str) -> str:
        if value == ".":
            return value
        return _validate_relative_build_path(value)

    @field_validator("dockerfile")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _validate_relative_build_path(value)

    @field_validator("requirements")
    @classmethod
    def safe_optional_path(cls, value: str) -> str:
        return _validate_relative_build_path(value) if value else value

    @field_validator("env_var")
    @classmethod
    def valid_environment_name(cls, value: str) -> str:
        if not _SECRET_NAME.fullmatch(value):
            raise ValueError("image environment variable is invalid")
        return value

    @field_validator("platforms")
    @classmethod
    def valid_platforms(
        cls, values: tuple[Literal["linux/amd64", "linux/arm64"], ...]
    ) -> tuple[Literal["linux/amd64", "linux/arm64"], ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("image build platforms must be a non-empty unique list")
        return values

    @model_validator(mode="after")
    def valid_scope(self) -> ImageBuildManifest:
        if self.repository_context and self.context != ".":
            raise ValueError("repository image builds must use the repository root context")
        if not self.repository_context and self.context == ".":
            raise ValueError("service image builds must name a service-relative context")
        return self


class ImageReleaseManifest(StrictManifestModel):
    compose_service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    repository: str = Field(min_length=3, max_length=256)
    version: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        if value != value.lower() or "@" in value or any(character.isspace() for character in value):
            raise ValueError("release image repository must be a lowercase registry path")
        parts = value.split("/")
        registry = parts[0]
        if (
            len(parts) < 3
            or any(not part for part in parts)
            or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?", registry)
            or any(not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", part) for part in parts[1:])
        ):
            raise ValueError("release image repository must be an explicit registry path")
        return value

    @property
    def version_ref(self) -> str:
        return f"{self.repository}:{self.version}"

    @property
    def immutable_ref(self) -> str:
        return f"{self.version_ref}@{self.digest}"


class IntegrationContractManifest(StrictManifestModel):
    """Versioned HTTP boundary implemented by an independently released module."""

    version: int = Field(ge=1, le=999)
    compatibility: str = Field(pattern=r"^[1-9][0-9]{0,2}\.x$")
    endpoint: str = "/api/contract"
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return _validate_absolute_path(value)

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SERVICE_NAME.fullmatch(value) for value in values):
            raise ValueError("integration capabilities must be unique capability identifiers")
        return values

    @model_validator(mode="after")
    def matching_compatibility_major(self) -> IntegrationContractManifest:
        if self.compatibility != f"{self.version}.x":
            raise ValueError("integration compatibility must match the declared contract version")
        return self


class ServiceManifest(StrictManifestModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    placement: NodeId
    icon: str = Field(min_length=1, max_length=100)
    priority: int = Field(ge=0, le=10_000)
    # Host guest hooks run after service deployment and may have ordering
    # dependencies (for example, the Wazuh manager must precede its agents).
    # Keep this separate from ``priority`` so compose/start ordering remains
    # independent from host bootstrap ordering.
    guest_task_order: int = Field(default=1_000, ge=0, le=10_000)
    # Recovery can have a different dependency order from first bootstrap
    # (directory and security repair precede the manager repair).
    recovery_task_order: int = Field(default=1_000, ge=0, le=10_000)
    essential: bool = False
    restart_policy: Literal["safe", "careful", "never"] = "careful"
    depends_on: tuple[str, ...] = ()
    provides: tuple[str, ...] = Field(default=(), max_length=16)
    memory_tier: Literal["heavy", "medium", "light", "auto"] = "medium"
    memory_floor_mb: int = Field(default=128, ge=64, le=65_536)
    cpu_floor: float = Field(default=0.1, ge=0.05, le=64)
    runtime: Literal["container", "embedded"] = "container"
    runtimes: dict[str, RuntimeServiceManifest] = Field(default_factory=dict)
    host_sources: dict[str, HostSourceManifest] = Field(default_factory=dict)
    generated_artifacts: tuple[GeneratedArtifactManifest, ...] = Field(default=(), max_length=32)
    image_build: ImageBuildManifest | None = None
    image_release: ImageReleaseManifest | None = None
    integration_contract: IntegrationContractManifest | None = None
    stateful: bool = False
    plugin_module: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    enabled_when: tuple[ConfigPredicate, ...] = ()
    routes: tuple[RouteManifest, ...] = ()
    internal_aliases: tuple[str, ...] = Field(default=(), max_length=16)
    oidc: OIDCManifest | None = None
    data_specs: tuple[DataManifest, ...] = ()
    backup_exports: tuple[BackupExportManifest, ...] = ()
    host_paths: tuple[HostPathManifest, ...] = ()
    required_secrets: tuple[RequiredSecretManifest, ...] = ()
    secret_projections: tuple[SecretProjectionManifest, ...] = Field(default=(), max_length=16)
    runtime_variables: tuple[str, ...] = Field(default=(), max_length=32)
    credentials: tuple[CredentialManifest, ...] = ()
    health: HealthManifest = Field(default_factory=HealthManifest)
    guidance: tuple[OperatorGuidanceManifest, ...] = Field(default=(), max_length=16)
    operator_bookmark: OperatorBookmarkManifest | None = None
    identity: ServiceIdentityManifest = Field(default_factory=ServiceIdentityManifest)
    prometheus: tuple[PrometheusScrapeManifest, ...] = Field(default=(), max_length=16)
    database_provider: DatabaseProviderManifest | None = None
    databases: tuple[DatabaseBindingManifest, ...] = Field(default=(), max_length=16)
    service_endpoint: ServiceEndpointManifest | None = None
    integrations: tuple[ServiceIntegrationManifest, ...] = Field(default=(), max_length=32)
    network_listeners: tuple[NetworkListenerManifest, ...] = Field(default=(), max_length=32)
    management: ServiceManagement = Field(default_factory=ServiceManagement)
    host_integrations: tuple[HostIntegrationManifest, ...] = Field(default=(), max_length=8)
    variables: dict[str, str] = Field(default_factory=dict)

    @field_validator("placement")
    @classmethod
    def valid_placement(cls, value: str) -> str:
        from toolkit.core.machines.models import validate_machine_id

        return validate_machine_id(value)

    @field_validator("depends_on")
    @classmethod
    def valid_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SERVICE_NAME.fullmatch(value) for value in values):
            raise ValueError("service dependencies must be unique service names")
        return values

    @field_validator("internal_aliases")
    @classmethod
    def valid_internal_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SUBDOMAIN.fullmatch(value) for value in values):
            raise ValueError("internal DNS aliases must be unique DNS labels")
        return values

    @field_validator("provides")
    @classmethod
    def valid_provided_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SERVICE_NAME.fullmatch(value) for value in values):
            raise ValueError("provided capabilities must be unique capability identifiers")
        return values

    @field_validator("runtime_variables")
    @classmethod
    def valid_runtime_variables(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _SECRET_NAME.fullmatch(value) for value in values):
            raise ValueError("runtime variables must be unique environment names")
        return values

    @field_validator("runtimes")
    @classmethod
    def valid_runtimes(cls, values: dict[str, RuntimeServiceManifest]) -> dict[str, RuntimeServiceManifest]:
        for service in values:
            if not _SERVICE_NAME.fullmatch(service):
                raise ValueError("runtime keys must be service names")
        return values

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not _SECRET_NAME.fullmatch(name):
                raise ValueError("manifest variable names must be environment names")
            if len(value) > 4_096 or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("manifest variable values must be bounded text without control characters")
            for path in _CONFIG_VARIABLE.findall(value):
                if any(part.startswith("_") for part in path.split(".")):
                    raise ValueError("manifest variable config paths must contain public identifiers")
            remainder = _DERIVED_VARIABLE.sub(
                "",
                _SERVICE_VARIABLE.sub("", _SETTING_VARIABLE.sub("", _CONFIG_VARIABLE.sub("", value))),
            )
            if "{" in remainder or "}" in remainder:
                raise ValueError("manifest variables support only typed config, setting, and service templates")
        return values

    @field_validator("host_sources")
    @classmethod
    def safe_host_source_names(cls, values: dict[str, HostSourceManifest]) -> dict[str, HostSourceManifest]:
        if any(not _SECRET_NAME.fullmatch(name) for name in values):
            raise ValueError("host source names must be environment names")
        return values

    @field_validator("generated_artifacts")
    @classmethod
    def unique_generated_artifacts(
        cls, values: tuple[GeneratedArtifactManifest, ...]
    ) -> tuple[GeneratedArtifactManifest, ...]:
        paths = [artifact.path for artifact in values]
        if len(paths) != len(set(paths)):
            raise ValueError("generated artifact paths must be unique")
        return values

    @model_validator(mode="after")
    def declared_setting_variables(self) -> ServiceManifest:
        declared = {setting.key for setting in self.management.settings}
        referenced = {key for template in self.variables.values() for key in _SETTING_VARIABLE.findall(template)}
        unknown = sorted(referenced - declared)
        if unknown:
            raise ValueError("manifest variables reference undeclared settings: " + ", ".join(unknown))
        return self

    @model_validator(mode="after")
    def declared_management_secrets(self) -> ServiceManifest:
        declared = {secret.name for secret in self.required_secrets}
        unknown = sorted(secret.name for secret in self.management.secrets if secret.name not in declared)
        if unknown:
            raise ValueError("management secrets reference undeclared service secrets: " + ", ".join(unknown))
        return self

    @model_validator(mode="after")
    def safe_restart_policy(self) -> ServiceManifest:
        if self.image_build is not None and self.image_release is not None:
            raise ValueError("exactly one image ownership contract may be declared")
        if self.integration_contract is not None and self.image_release is None:
            raise ValueError("integration contracts require an independently released image")
        if self.essential and self.restart_policy == "safe":
            raise ValueError("essential services cannot declare an unconditional safe restart policy")
        prometheus_metrics = [metric for metric in self.management.metrics if metric.source == "prometheus"]
        if prometheus_metrics and not self.prometheus:
            raise ValueError("Prometheus management metrics require a typed scrape endpoint")
        if self.stateful and not self.data_specs:
            raise ValueError("stateful services must declare at least one storage asset")
        if self.data_specs and not self.stateful:
            raise ValueError("services with storage assets must be stateful")
        if self.backup_exports and not self.stateful:
            raise ValueError("services with backup exports must be stateful")
        if self.health.public_probe_path and not any(
            route.exposure == "public" and route.match is None for route in self.routes
        ):
            raise ValueError("a public health probe requires a default public route")
        guidance_ids = [entry.id for entry in self.guidance]
        if len(guidance_ids) != len(set(guidance_ids)):
            raise ValueError("operator guidance IDs must be unique within a service")
        if any(entry.route_url for entry in self.guidance) and sum(route.match is None for route in self.routes) != 1:
            raise ValueError("operator guidance URLs require exactly one default route")
        if self.identity.invite is not None and sum(route.match is None for route in self.routes) != 1:
            raise ValueError("invite cards require exactly one default route")
        if self.operator_bookmark is not None:
            default_routes = [route for route in self.routes if route.match is None]
            selector = self.operator_bookmark.route_subdomain
            if selector is None and len(default_routes) != 1:
                raise ValueError("operator bookmark requires exactly one default route")
            if selector is not None and not any(
                (route.subdomain if route.subdomain is not None else self.name) == selector for route in default_routes
            ):
                raise ValueError("operator bookmark route selector does not match a default route")
        binding_keys = [(binding.provider, binding.database, binding.username) for binding in self.databases]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("database bindings must be unique within a service")
        integration_services = [integration.service for integration in self.integrations]
        if len(integration_services) != len(set(integration_services)):
            raise ValueError("service integrations must be unique within a service")
        integration_environments = [
            value
            for integration in self.integrations
            for value in (
                integration.enabled_env,
                integration.host_env,
                integration.port_env,
                integration.address_env,
                integration.url_env,
            )
            if value is not None
        ]
        if len(integration_environments) != len(set(integration_environments)):
            raise ValueError("service integration environment outputs must be unique within a service")
        projection_targets = [projection.target_env for projection in self.secret_projections]
        if len(projection_targets) != len(set(projection_targets)):
            raise ValueError("secret projection targets must be unique within a service")
        declared_environments = set(self.variables) | {secret.name for secret in self.required_secrets}
        undeclared_passwords = sorted(
            binding.password_env for binding in self.databases if binding.password_env not in declared_environments
        )
        if undeclared_passwords:
            raise ValueError(
                "database binding references undeclared password environment: " + ", ".join(undeclared_passwords)
            )
        if self.database_provider is not None:
            if self.service_endpoint is None:
                raise ValueError("database provider requires a service endpoint")
            provider_environments = {
                self.database_provider.admin_username_env,
                self.database_provider.admin_password_env,
                self.database_provider.admin_database_env,
            }
            missing_provider_environments = sorted(provider_environments - declared_environments)
            if missing_provider_environments:
                raise ValueError(
                    "database provider references undeclared administrator environment: "
                    + ", ".join(missing_provider_environments)
                )
        names = [asset.name for asset in self.data_specs]
        if len(names) != len(set(names)):
            raise ValueError("storage asset names must be unique within a service")
        backup_artifacts = [export.artifact for export in self.backup_exports]
        if len(backup_artifacts) != len(set(backup_artifacts)):
            raise ValueError("backup export artifacts must be unique within a service")
        if any(not artifact.startswith(f"{self.name}.") for artifact in backup_artifacts):
            raise ValueError("backup export artifacts must be prefixed by the service name")
        data_by_name = {asset.name: asset for asset in self.data_specs}
        for export in self.backup_exports:
            if export.strategy != "sqlite":
                continue
            asset = data_by_name.get(export.data_spec)
            if asset is None:
                raise ValueError(f"backup export references unknown storage asset {export.data_spec!r}")
            if asset.source_env is None:
                raise ValueError("SQLite backup exports require a bind-mounted storage asset")
        undeclared_runtimes = sorted(
            {asset.runtime_service for asset in self.data_specs if asset.runtime_service} - set(self.runtimes)
        )
        undeclared_runtimes.extend(
            sorted(
                {export.runtime_service for export in self.backup_exports if export.runtime_service}
                - set(self.runtimes)
            )
        )
        referenced_runtimes = {
            listener.runtime_service
            for listener in self.network_listeners
            if listener.runtime_service and listener.runtime_service != self.name
        } | {scrape.runtime_service for scrape in self.prometheus if scrape.runtime_service}
        referenced_runtimes.update(
            artifact.runtime_service for artifact in self.generated_artifacts if artifact.runtime_service
        )
        undeclared_runtimes.extend(sorted(referenced_runtimes - set(self.runtimes)))
        if undeclared_runtimes:
            raise ValueError("referenced runtime is not declared: " + ", ".join(sorted(set(undeclared_runtimes))))
        paths = [entry.path for entry in self.host_paths]
        if len(paths) != len(set(paths)):
            raise ValueError("host paths must be unique within a service")
        integration_ids = [integration.id for integration in self.host_integrations]
        if len(integration_ids) != len(set(integration_ids)):
            raise ValueError("host integration identifiers must be unique within a service")
        listener_ids = [listener.id for listener in self.network_listeners]
        if len(listener_ids) != len(set(listener_ids)):
            raise ValueError("network listener identifiers must be unique within a service")
        runtime_sources = {
            source.removeprefix("@runtime:")
            for listener in self.network_listeners
            for source in listener.sources
            if source.startswith("@runtime:")
        }
        unknown_runtime_sources = sorted(runtime_sources - set(self.runtimes))
        if unknown_runtime_sources:
            raise ValueError("network listener source runtime is not declared: " + ", ".join(unknown_runtime_sources))
        scrape_ids = [scrape.id for scrape in self.prometheus]
        if len(scrape_ids) != len(set(scrape_ids)):
            raise ValueError("Prometheus scrape identifiers must be unique within a service")
        return self
