"""Authenticated encryption for controller job fields that must survive restarts."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from toolkit.controller.contracts import InviteUserCommand, SealedInviteUserCommand

_KEY_BYTES = 32
_NONCE_BYTES = 12
_AAD_VERSION = b"homelab-controller-invite-v1"
_KEY_CHECK_PLAINTEXT = b"homelab-controller-payload-key-v1"
_KEY_CHECK_AAD = b"homelab-controller-payload-key-check-v1"


class PayloadKeyError(RuntimeError):
    pass


class PayloadProtectionError(RuntimeError):
    pass


def payload_key_path(database_path: Path) -> Path:
    configured = os.environ.get("HOMELAB_CONTROLLER_PAYLOAD_KEY_FILE", "").strip()
    return Path(configured).expanduser() if configured else database_path.parent / "controller-payload.key"


def load_or_create_payload_key(database_path: Path) -> tuple[Path, bytes]:
    path = payload_key_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_exists = database_path.exists()
    try:
        key = _read_payload_key(path)
    except FileNotFoundError:
        if database_exists:
            raise PayloadKeyError("controller payload key is missing for an existing database") from None
        key = os.urandom(_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            key = _read_payload_key(path)
        else:
            try:
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    _verify_or_create_key_check(path, key, allow_create=not database_exists)
    return path, key


def _verify_or_create_key_check(key_path: Path, key: bytes, *, allow_create: bool) -> None:
    check_path = key_path.with_name(f"{key_path.name}.check")
    try:
        protected = _read_secure_file(check_path, max_bytes=256)
    except FileNotFoundError:
        if not allow_create:
            raise PayloadKeyError("controller payload key check is missing for an existing database") from None
        nonce = os.urandom(_NONCE_BYTES)
        protected = nonce + AESGCM(key).encrypt(nonce, _KEY_CHECK_PLAINTEXT, _KEY_CHECK_AAD)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(check_path, flags, 0o600)
        except FileExistsError:
            protected = _read_secure_file(check_path, max_bytes=256)
        else:
            try:
                os.write(descriptor, protected)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    try:
        plaintext = AESGCM(key).decrypt(protected[:_NONCE_BYTES], protected[_NONCE_BYTES:], _KEY_CHECK_AAD)
    except (InvalidTag, ValueError):
        raise PayloadKeyError("controller payload key does not match its key check") from None
    if plaintext != _KEY_CHECK_PLAINTEXT:
        raise PayloadKeyError("controller payload key check is invalid")


def _read_payload_key(path: Path) -> bytes:
    value = _read_secure_file(path, max_bytes=_KEY_BYTES + 1)
    if len(value) != _KEY_BYTES:
        raise PayloadKeyError("controller payload key must contain exactly 32 bytes")
    return value


def _read_secure_file(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise PayloadKeyError("controller payload key cannot be opened safely") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PayloadKeyError("controller payload key must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PayloadKeyError("controller payload key must have mode 0600")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PayloadKeyError("controller payload key must be owned by the controller user")
        value = os.read(descriptor, max_bytes)
    finally:
        os.close(descriptor)
    return value


def _aad(principal: str, idempotency_key: str) -> bytes:
    return b"\0".join((_AAD_VERSION, principal.encode("utf-8"), idempotency_key.encode("utf-8")))


def seal_invite_command(
    command: InviteUserCommand,
    *,
    key: bytes,
    principal: str,
    idempotency_key: str,
) -> SealedInviteUserCommand:
    plaintext = json.dumps(
        command.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    nonce = os.urandom(_NONCE_BYTES)
    protected = nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(principal, idempotency_key))
    return SealedInviteUserCommand(ciphertext=base64.urlsafe_b64encode(protected).decode("ascii"))


def open_invite_command(
    command: SealedInviteUserCommand,
    *,
    key: bytes,
    principal: str,
    idempotency_key: str,
) -> InviteUserCommand:
    try:
        protected = base64.b64decode(command.ciphertext, altchars=b"-_", validate=True)
        nonce, ciphertext = protected[:_NONCE_BYTES], protected[_NONCE_BYTES:]
        if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
            raise ValueError
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(principal, idempotency_key))
        return InviteUserCommand.model_validate_json(plaintext)
    except (InvalidTag, ValueError, ValidationError):
        raise PayloadProtectionError("controller invite payload could not be authenticated") from None
