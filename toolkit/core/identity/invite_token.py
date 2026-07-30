"""One-time invite activation tokens (Redis-backed, signed URL)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

if TYPE_CHECKING:
    from redis import Redis

TOKEN_MAX_AGE_SECONDS = 72 * 3600
REDIS_CONNECT_TIMEOUT_SECONDS = 2
REDIS_READ_TIMEOUT_SECONDS = 3
_REDIS_PREFIX = "homelab:invite:"
_SUBJECT_PREFIX = "homelab:invite:subject:"
_ACTIVATION_PREFIX = "homelab:invite:activation:"
_DELIVERY_PREFIX = "homelab:invite:delivery:"
_USER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_USERS = frozenset({"admin", "ldap-bind"})

_ISSUE_SCRIPT = """
-- INVITE_ISSUE
if KEYS[3] ~= '' then
  local cached = redis.call('GET', KEYS[3])
  if cached then
    return 'CACHED:' .. cached
  end
end
local previous = redis.call('GET', KEYS[2])
if previous then
  redis.call('DEL', ARGV[4] .. previous)
end
redis.call('SETEX', KEYS[1], ARGV[2], ARGV[3])
redis.call('SETEX', KEYS[2], ARGV[2], ARGV[1])
if KEYS[3] ~= '' then
  redis.call('SETEX', KEYS[3], ARGV[2], ARGV[5])
end
return 'ISSUED'
"""
_PEEK_SCRIPT = """
-- INVITE_PEEK
local value = redis.call('GET', KEYS[1])
if not value or redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return nil
end
return value
"""
_ACTIVATION_BEGIN_SCRIPT = """
-- INVITE_ACTIVATION_BEGIN
local state = redis.call('GET', KEYS[3])
if state then
  return 'STATE:' .. state
end
local value = redis.call('GET', KEYS[1])
if not value or redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return nil
end
redis.call('SETEX', KEYS[3], ARGV[3], 'ACTIVATING:' .. ARGV[2])
redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])
return value
"""
_ACTIVATION_FINISH_SCRIPT = """
-- INVITE_ACTIVATION_FINISH
if redis.call('GET', KEYS[1]) ~= 'ACTIVATING:' .. ARGV[1] then
  return 0
end
redis.call('SETEX', KEYS[1], ARGV[3], ARGV[2])
return 1
"""


ActivationState = Literal["acquired", "activating", "succeeded", "failed", "invalid"]


@dataclass(frozen=True, repr=False)
class InviteActivation:
    state: ActivationState
    payload: dict[str, Any]
    activation_id: str


def _invite_secret(secrets: dict[str, str]) -> str:
    value = secrets.get("INVITE_TOKEN_SECRET", "").strip()
    if len(value) >= 32:
        return value
    if value:
        raise RuntimeError("INVITE_TOKEN_SECRET must contain at least 32 characters")
    raise RuntimeError("INVITE_TOKEN_SECRET missing — run secrets generate")


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret,
        salt="homelab-invite-v2",
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha256},
    )


def _redis_client() -> Redis:
    import redis

    from toolkit.services.sdk.redis import redis_port

    host = os.environ.get("HOMELAB_REDIS_HOST", "redis")
    port = int(os.environ.get("HOMELAB_REDIS_PORT", str(redis_port())))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    return redis.Redis(
        host=host,
        port=port,
        password=password,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_READ_TIMEOUT_SECONDS,
        health_check_interval=30,
    )


_PAYLOAD_KEYS = frozenset({"email", "user_id", "display_name", "groups"})


def _validated_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _PAYLOAD_KEYS:
        return None
    email = value.get("email")
    user_id = value.get("user_id")
    display_name = value.get("display_name")
    groups = value.get("groups")
    if (
        not isinstance(email, str)
        or not email
        or len(email) > 254
        or email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or _CONTROL.search(email)
    ):
        return None
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id) or user_id in _RESERVED_USERS:
        return None
    if not isinstance(display_name, str) or len(display_name) > 128 or _CONTROL.search(display_name):
        return None
    if (
        not isinstance(groups, list)
        or len(groups) > 64
        or any(not isinstance(group, str) or not _GROUP.fullmatch(group) for group in groups)
        or len(set(groups)) != len(groups)
    ):
        return None
    return {
        "email": email,
        "user_id": user_id,
        "display_name": display_name,
        "groups": groups,
    }


def _parse_payload(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, str | bytes | bytearray):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return _validated_payload(payload)


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _subject_id(payload: dict[str, Any]) -> str:
    return hashlib.sha256(str(payload["user_id"]).encode("utf-8")).hexdigest()


def invite_csrf_token(secrets: dict[str, str], token: str) -> str:
    """Derive a form nonce that is cryptographically bound to one invite bearer."""
    return hmac.new(
        _invite_secret(secrets).encode("utf-8"),
        b"homelab-invite-csrf-v1\0" + token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_invite_csrf(secrets: dict[str, str], token: str, supplied: str) -> bool:
    if not _HEX_DIGEST.fullmatch(supplied):
        return False
    return hmac.compare_digest(invite_csrf_token(secrets, token), supplied)


def _signed_token_data(secrets: dict[str, str], token: str) -> tuple[str, str, str] | None:
    try:
        data = _serializer(_invite_secret(secrets)).loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or set(data) != {"v", "jti", "subject", "payload_sha256"}:
        return None
    version = data.get("v")
    jti = data.get("jti")
    subject = data.get("subject")
    payload_sha256 = data.get("payload_sha256")
    if (
        type(version) is not int
        or version != 2
        or not isinstance(jti, str)
        or not _JTI.fullmatch(jti)
        or not isinstance(subject, str)
        or not _HEX_DIGEST.fullmatch(subject)
        or not isinstance(payload_sha256, str)
        or not _HEX_DIGEST.fullmatch(payload_sha256)
    ):
        return None
    return jti, subject, payload_sha256


def _payload_matches(stored: dict[str, Any], subject: str, payload_sha256: str) -> bool:
    return hmac.compare_digest(_subject_id(stored), subject) and hmac.compare_digest(
        _payload_digest(stored), payload_sha256
    )


def create_invite_token(
    secrets: dict[str, str],
    *,
    email: str,
    user_id: str,
    display_name: str | None,
    groups: list[str],
    issuance_id: str | None = None,
) -> str:
    """Store invite payload in Redis and return a signed activation token."""
    import secrets as py_secrets

    secret = _invite_secret(secrets)
    candidate = {
        "email": email.strip().lower(),
        "user_id": user_id.strip(),
        "display_name": (display_name or "").strip(),
        "groups": [group.strip() for group in groups],
    }
    payload = _validated_payload(candidate)
    if payload is None:
        raise ValueError("invite payload is invalid")
    subject = _subject_id(payload)
    payload_sha256 = _payload_digest(payload)
    delivery_key = ""
    delivery_record = ""
    if issuance_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", issuance_id):
            raise ValueError("invite issuance id is invalid")
        delivery_key = f"{_DELIVERY_PREFIX}{hashlib.sha256(issuance_id.encode('utf-8')).hexdigest()}"
    jti = py_secrets.token_urlsafe(18)
    token = _serializer(secret).dumps({"v": 2, "jti": jti, "subject": subject, "payload_sha256": payload_sha256})
    if delivery_key:
        delivery_record = json.dumps(
            {"payload_sha256": payload_sha256, "token": token},
            sort_keys=True,
            separators=(",", ":"),
        )
    result = _redis_client().eval(
        _ISSUE_SCRIPT,
        3,
        f"{_REDIS_PREFIX}{jti}",
        f"{_SUBJECT_PREFIX}{subject}",
        delivery_key,
        jti,
        str(TOKEN_MAX_AGE_SECONDS),
        _canonical_payload(payload),
        _REDIS_PREFIX,
        delivery_record,
    )
    if isinstance(result, str) and result.startswith("CACHED:"):
        try:
            existing = json.loads(result.removeprefix("CACHED:"))
        except json.JSONDecodeError:
            existing = None
        if not isinstance(existing, dict) or set(existing) != {"payload_sha256", "token"}:
            raise RuntimeError("cached invite delivery record is invalid")
        if not hmac.compare_digest(str(existing["payload_sha256"]), payload_sha256):
            raise ValueError("invite issuance id was already used for another payload")
        cached_token = existing["token"]
        if not isinstance(cached_token, str):
            raise RuntimeError("cached invite delivery token is invalid")
        return cached_token
    return token


def peek_invite_token(secrets: dict[str, str], token: str) -> dict[str, Any] | None:
    """Validate token and return payload without consuming (for GET form)."""
    token_data = _signed_token_data(secrets, token)
    if token_data is None:
        return None
    jti, subject, payload_sha256 = token_data
    raw = _redis_client().eval(
        _PEEK_SCRIPT,
        2,
        f"{_REDIS_PREFIX}{jti}",
        f"{_SUBJECT_PREFIX}{subject}",
        jti,
    )
    if not raw:
        return None
    stored_payload = _parse_payload(raw)
    if stored_payload is None or not _payload_matches(stored_payload, subject, payload_sha256):
        return None
    return stored_payload


def begin_invite_activation(secrets: dict[str, str], token: str) -> InviteActivation:
    """Consume an issued invite into a non-reopenable activation state."""
    import secrets as py_secrets

    token_data = _signed_token_data(secrets, token)
    if token_data is None:
        return InviteActivation(state="invalid", payload={}, activation_id="")
    jti, subject, payload_sha256 = token_data
    activation_id = py_secrets.token_urlsafe(24)
    raw = _redis_client().eval(
        _ACTIVATION_BEGIN_SCRIPT,
        3,
        f"{_REDIS_PREFIX}{jti}",
        f"{_SUBJECT_PREFIX}{subject}",
        f"{_ACTIVATION_PREFIX}{jti}",
        jti,
        activation_id,
        str(TOKEN_MAX_AGE_SECONDS),
    )
    if not raw:
        return InviteActivation(state="invalid", payload={}, activation_id="")
    if isinstance(raw, str) and raw.startswith("STATE:"):
        stored = raw.removeprefix("STATE:")
        if stored.startswith("ACTIVATING:"):
            return InviteActivation(state="activating", payload={}, activation_id="")
        if stored == "SUCCEEDED":
            return InviteActivation(state="succeeded", payload={}, activation_id="")
        if stored == "FAILED":
            return InviteActivation(state="failed", payload={}, activation_id="")
        return InviteActivation(state="invalid", payload={}, activation_id="")
    stored_payload = _parse_payload(raw)
    if stored_payload is None or not _payload_matches(stored_payload, subject, payload_sha256):
        complete_invite_activation(secrets, token, activation_id, succeeded=False)
        return InviteActivation(state="failed", payload={}, activation_id="")
    return InviteActivation(state="acquired", payload=stored_payload, activation_id=activation_id)


def complete_invite_activation(
    secrets: dict[str, str],
    token: str,
    activation_id: str,
    *,
    succeeded: bool,
) -> bool:
    """Record a terminal activation outcome without ever reopening the invite."""
    token_data = _signed_token_data(secrets, token)
    if token_data is None or not _JTI.fullmatch(activation_id):
        return False
    jti, _subject, _payload_sha256 = token_data
    return bool(
        _redis_client().eval(
            _ACTIVATION_FINISH_SCRIPT,
            1,
            f"{_ACTIVATION_PREFIX}{jti}",
            activation_id,
            "SUCCEEDED" if succeeded else "FAILED",
            str(TOKEN_MAX_AGE_SECONDS),
        )
    )
