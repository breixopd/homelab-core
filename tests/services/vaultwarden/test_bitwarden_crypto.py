import pytest
from cryptography.hazmat.primitives.serialization import load_der_private_key
from toolkit.core.secrets.bitwarden_crypto import (
    DEFAULT_KDF_ITERATIONS,
    KDF_ARGON2ID,
    KDF_PBKDF2,
    KdfParams,
    build_register_keys,
    decrypt_cipher_string,
    encrypt_cipher_string,
    make_master_key,
    make_master_password_hash,
    make_protected_symmetric_key,
    register_payload,
    stable_vaultwarden_admin_hash,
    unlock_account_keys,
)


def test_master_password_hash_rubywarden_vector():
    """Vector from jcs/rubywarden API.md (5000 iterations, PBKDF2)."""
    kdf = KdfParams(KDF_PBKDF2, 5000, 65536, 4)
    assert (
        make_master_password_hash("p4ssw0rd", "nobody@example.com", kdf)
        == "r5CFRR+n9NQI8a525FY+0BPR0HGOjVJX0cR1KEMnIOo="
    )


def test_master_password_hash_matches_current_bitwarden_argon2_vector():
    """Vector from bitwarden/sdk-internal master_key.rs."""
    kdf = KdfParams(KDF_ARGON2ID, 4, 32 * 1024, 2)

    assert make_master_password_hash("asdfasdf", "test_salt", kdf) == "PR6UjYmjmppTYcdyTiNbAhPJuQQOmynKbdEl1oyi/iQ="


def test_register_payload_matches_vaultwarden_1_36_contract():
    payload = register_payload("secret", "admin@example.com")
    assert payload["email"] == "admin@example.com"
    assert payload["kdf"] == KDF_ARGON2ID
    assert payload["kdfIterations"] == DEFAULT_KDF_ITERATIONS
    assert payload["kdfMemory"] == 32
    assert payload["key"].startswith("2.")
    assert payload["masterPasswordHash"]
    assert payload["keys"]["publicKey"]
    assert payload["keys"]["encryptedPrivateKey"]


def test_encrypt_decrypt_round_trip():
    keys = build_register_keys("vault-secret", "owner@example.com")
    plaintext = "super-secret-password"
    enc = encrypt_cipher_string(plaintext, keys.enc_key, keys.mac_key)
    assert enc.startswith("2.")
    assert decrypt_cipher_string(enc, keys.enc_key, keys.mac_key).decode("utf-8") == plaintext


def test_protected_symmetric_key_uses_authenticated_encryption():
    password = "vault-secret"
    email = "owner@example.com"
    kdf = KdfParams(KDF_ARGON2ID, DEFAULT_KDF_ITERATIONS, 65536, 4)
    master_key = make_master_key(password, email, kdf)
    protected, enc_key, mac_key = make_protected_symmetric_key(master_key)
    assert protected.startswith("2.")
    assert len(enc_key) == 32
    assert len(mac_key) == 32


def test_unlock_account_keys_matches_make_protected_symmetric_key():
    password = "vault-secret"
    email = "owner@example.com"
    kdf = KdfParams(KDF_ARGON2ID, DEFAULT_KDF_ITERATIONS, 65536, 4)
    master_key = make_master_key(password, email, kdf)
    protected, enc_key, mac_key = make_protected_symmetric_key(master_key)
    unlocked_enc, unlocked_mac = unlock_account_keys(password, email, protected, kdf)
    assert unlocked_enc == enc_key
    assert unlocked_mac == mac_key


def test_decrypt_cipher_string_rejects_bad_mac():
    keys = build_register_keys("vault-secret", "owner@example.com")
    enc = encrypt_cipher_string("value", keys.enc_key, keys.mac_key)
    parts = enc.split("|")
    bad_mac = "AAAA" + parts[2][4:]
    tampered = "|".join([parts[0], parts[1], bad_mac])
    with pytest.raises(ValueError, match="HMAC"):
        decrypt_cipher_string(tampered, keys.enc_key, keys.mac_key)


def test_private_key_is_pkcs8_der_wrapped_by_user_key():
    keys = build_register_keys("vault-secret", "owner@example.com")

    private_der = decrypt_cipher_string(keys.encrypted_private_key, keys.enc_key, keys.mac_key)

    assert load_der_private_key(private_der, password=None) is not None


def test_vaultwarden_admin_hash_is_reused_until_token_changes():
    first = stable_vaultwarden_admin_hash("initial-token")

    assert stable_vaultwarden_admin_hash("initial-token", first) == first
    assert stable_vaultwarden_admin_hash("changed-token", first) != first
    assert stable_vaultwarden_admin_hash("initial-token", "not-a-phc-hash").startswith("$argon2id$")
