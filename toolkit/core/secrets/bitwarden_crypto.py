"""Minimal Bitwarden/Vaultwarden account crypto (Argon2id + PBKDF2 + AES-CBC)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import NamedTuple

from argon2 import low_level
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.padding import PKCS7

# Bitwarden: 0 = PBKDF2-SHA256, 1 = Argon2id (preferred for new accounts).
KDF_PBKDF2 = 0
KDF_ARGON2ID = 1

DEFAULT_KDF = KDF_ARGON2ID
DEFAULT_KDF_ITERATIONS = 6
DEFAULT_KDF_MEMORY = 32768
DEFAULT_KDF_PARALLELISM = 4

PBKDF2_ITERATIONS = 600_000


class KdfParams(NamedTuple):
    kdf: int
    iterations: int
    memory: int
    parallelism: int


class RegisterKeys(NamedTuple):
    master_password_hash: str
    protected_symmetric_key: str
    enc_key: bytes
    mac_key: bytes
    public_key: str
    encrypted_private_key: str
    kdf: KdfParams


def _pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=length)


def _normalized_email(email: str) -> str:
    return email.strip().lower()


def make_master_key(password: str, email: str, kdf: KdfParams) -> bytes:
    salt = _normalized_email(email).encode("utf-8")
    pwd = password.encode("utf-8")
    if kdf.kdf == KDF_ARGON2ID:
        # Bitwarden hashes the normalized email before supplying it to Argon2.
        salt = hashlib.sha256(salt).digest()
        return low_level.hash_secret_raw(
            pwd,
            salt,
            time_cost=kdf.iterations,
            memory_cost=kdf.memory,
            parallelism=kdf.parallelism,
            hash_len=32,
            type=low_level.Type.ID,
        )
    return _pbkdf2_sha256(pwd, salt, kdf.iterations)


def make_master_password_hash(password: str, email: str, kdf: KdfParams) -> str:
    master_key = make_master_key(password, email, kdf)
    hashed = _pbkdf2_sha256(master_key, password.encode("utf-8"), 1)
    return base64.b64encode(hashed).decode("ascii")


def kdf_from_prelogin(data: dict) -> KdfParams:
    kdf = int(data.get("Kdf") or data.get("kdf") or KDF_PBKDF2)
    iterations = int(data.get("KdfIterations") or data.get("kdfIterations") or PBKDF2_ITERATIONS)
    memory = int(data.get("KdfMemory") or data.get("kdfMemory") or DEFAULT_KDF_MEMORY)
    parallelism = int(data.get("KdfParallelism") or data.get("kdfParallelism") or DEFAULT_KDF_PARALLELISM)
    if kdf == KDF_ARGON2ID and memory <= 1024:
        memory *= 1024  # prelogin returns MiB; internal KdfParams uses KiB for argon2
    if kdf == KDF_PBKDF2 and iterations < 1000:
        iterations = PBKDF2_ITERATIONS
    if kdf == KDF_ARGON2ID and iterations < 1:
        iterations = DEFAULT_KDF_ITERATIONS
    return KdfParams(kdf, iterations, memory, parallelism)


def _cipher_string(enc_type: int, iv: bytes, ciphertext: bytes, mac: bytes | None) -> str:
    parts = [f"{enc_type}.{base64.b64encode(iv).decode('ascii')}"]
    parts.append(base64.b64encode(ciphertext).decode("ascii"))
    if mac is not None:
        parts.append(base64.b64encode(mac).decode("ascii"))
    return "|".join(parts)


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _stretch_master_key(master_key: bytes) -> tuple[bytes, bytes]:
    """Expand a Bitwarden master key into AES and HMAC keys."""
    enc_key = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"enc").derive(master_key)
    mac_key = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"mac").derive(master_key)
    return enc_key, mac_key


def make_protected_symmetric_key(master_key: bytes) -> tuple[str, bytes, bytes]:
    """Return a current Bitwarden user key wrapped by the stretched master key."""
    symmetric = secrets.token_bytes(64)
    enc_key = symmetric[:32]
    mac_key = symmetric[32:]
    master_enc_key, master_mac_key = _stretch_master_key(master_key)
    protected = _encrypt_bytes(symmetric, master_enc_key, master_mac_key)
    return protected, enc_key, mac_key


def encrypt_cipher_string(plaintext: str, enc_key: bytes, mac_key: bytes) -> str:
    return _encrypt_bytes(plaintext.encode("utf-8"), enc_key, mac_key)


def _encrypt_bytes(plaintext: bytes, enc_key: bytes, mac_key: bytes) -> str:
    iv = secrets.token_bytes(16)
    ciphertext = _aes_cbc_encrypt(enc_key, iv, plaintext)
    mac = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return _cipher_string(2, iv, ciphertext, mac)


def decrypt_cipher_string(cipher_string: str, key: bytes, mac_key: bytes | None = None) -> bytes:
    """Decrypt a current authenticated Bitwarden EncString."""
    parts = cipher_string.split("|")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid cipher string: {cipher_string!r}")

    enc_type_part, iv_b64 = parts[0].split(".", 1)
    enc_type = int(enc_type_part)
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(parts[1])

    if enc_type == 2:
        if mac_key is None:
            raise ValueError("mac_key required for enc_type 2")
        if len(parts) != 3:
            raise ValueError("enc_type 2 requires mac segment")
        mac = base64.b64decode(parts[2])
        expected = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("HMAC verification failed")
        return _aes_cbc_decrypt(key, iv, ciphertext)

    raise ValueError(f"unsupported enc_type: {enc_type}")


def unlock_account_keys(
    master_password: str,
    email: str,
    protected_symmetric_key: str,
    kdf: KdfParams,
) -> tuple[bytes, bytes]:
    """Derive enc_key and mac_key from master password and account protected key."""
    master_key = make_master_key(master_password, email, kdf)
    master_enc_key, master_mac_key = _stretch_master_key(master_key)
    symmetric = decrypt_cipher_string(protected_symmetric_key, master_enc_key, master_mac_key)
    if len(symmetric) != 64:
        raise ValueError(f"expected 64-byte symmetric key, got {len(symmetric)}")
    return symmetric[:32], symmetric[32:]


def _generate_rsa_keypair() -> tuple[bytes, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, base64.b64encode(public_der).decode("ascii")


def build_register_keys(
    password: str,
    email: str,
    *,
    kdf: KdfParams | None = None,
) -> RegisterKeys:
    params = kdf or KdfParams(DEFAULT_KDF, DEFAULT_KDF_ITERATIONS, DEFAULT_KDF_MEMORY, DEFAULT_KDF_PARALLELISM)
    master_key = make_master_key(password, email, params)
    protected, enc_key, mac_key = make_protected_symmetric_key(master_key)
    private_pem, public_b64 = _generate_rsa_keypair()
    encrypted_private = _encrypt_bytes(private_pem, enc_key, mac_key)
    return RegisterKeys(
        master_password_hash=make_master_password_hash(password, email, params),
        protected_symmetric_key=protected,
        enc_key=enc_key,
        mac_key=mac_key,
        public_key=public_b64,
        encrypted_private_key=encrypted_private,
        kdf=params,
    )


def register_payload(
    password: str,
    email: str,
    *,
    name: str | None = None,
    kdf: KdfParams | None = None,
) -> dict:
    params = kdf or KdfParams(DEFAULT_KDF, DEFAULT_KDF_ITERATIONS, DEFAULT_KDF_MEMORY, DEFAULT_KDF_PARALLELISM)
    keys = build_register_keys(password, email, kdf=params)
    payload: dict = {
        "name": name or email.split("@", 1)[0],
        "email": _normalized_email(email),
        "masterPasswordHint": None,
        "masterPasswordHash": keys.master_password_hash,
        "key": keys.protected_symmetric_key,
        "kdf": params.kdf,
        "kdfIterations": params.iterations,
        "keys": {
            "publicKey": keys.public_key,
            "encryptedPrivateKey": keys.encrypted_private_key,
        },
    }
    if params.kdf == KDF_ARGON2ID:
        payload["kdfMemory"] = params.memory // 1024 if params.memory >= 1024 else params.memory
        payload["kdfParallelism"] = params.parallelism
    return payload


def stable_vaultwarden_admin_hash(plain_token: str, existing_hash: str = "") -> str:
    """Return a valid Argon2id admin-token hash without needless salt churn."""
    if plain_token.startswith("$argon2"):
        return plain_token
    if existing_hash.startswith("$argon2"):
        try:
            if low_level.verify_secret(
                existing_hash.encode("ascii"),
                plain_token.encode("utf-8"),
                low_level.Type.ID,
            ):
                return existing_hash
        except (InvalidHashError, VerificationError, UnicodeError):
            pass
    return low_level.hash_secret(
        plain_token.encode("utf-8"),
        secrets.token_bytes(16),
        time_cost=DEFAULT_KDF_ITERATIONS,
        memory_cost=DEFAULT_KDF_MEMORY,
        parallelism=DEFAULT_KDF_PARALLELISM,
        hash_len=32,
        type=low_level.Type.ID,
        version=19,
    ).decode("ascii")


def encode_account_keys(enc_key: bytes, mac_key: bytes) -> tuple[str, str]:
    return base64.b64encode(enc_key).decode("ascii"), base64.b64encode(mac_key).decode("ascii")
