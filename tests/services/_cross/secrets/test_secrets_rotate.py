"""secrets.rotate_secrets must not blank SSH keys (length==0) and must
honor hex_only. Today it calls generate_secret(spec.length) unconditionally —
SSH keys have length==0, so generate_secret(0) returns '' and overwrites the
real key.
"""

from __future__ import annotations

from pathlib import Path

from toolkit.core.config.storage import secrets_path
from toolkit.core.secrets.secrets import rotate_secrets, save_secrets_plaintext


def _bootstrap_secrets(root: Path) -> None:
    """Write a config.yaml + secrets.enc.yaml with a real SSH key placeholder."""
    (root / "config.yaml").write_text(
        "domain: example.com\nemail: a@b.com\nservice_settings:\n"
        "  media-library:\n    server: jellyfin\n"
        "  tdarr:\n    enabled: false\n"
        "  media-cache:\n    enabled: false\n"
    )
    save_secrets_plaintext(
        {
            "HOMELAB_SSH_PRIVATE_KEY": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nreal-key-material\n-----END OPENSSH PRIVATE KEY-----"
            ),
            "HOMELAB_SSH_PUBLIC_KEY": "ssh-ed25519 AAAA real-public-key",
            "NEXTCLOUD_ADMIN_PASSWORD": "old-pw-12345678",
        },
        secrets_path(root),
    )


def test_rotate_does_not_blank_ssh_private_key(tmp_path: Path):
    _bootstrap_secrets(tmp_path)
    # Rotate management-category secrets (includes the SSH key specs).
    rotate_secrets(tmp_path)
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    finals = load_secrets_plaintext(secrets_path(tmp_path))
    # CRITICAL: the SSH private key must NOT be blanked by generate_secret(0).
    assert "real-key-material" in finals["HOMELAB_SSH_PRIVATE_KEY"], (
        "rotate_secrets blanked the SSH private key — generate_secret(0) returned '' "
        "and overwrote the real key. Data loss."
    )


def test_rotate_does_not_blank_ssh_public_key(tmp_path: Path):
    _bootstrap_secrets(tmp_path)
    rotate_secrets(tmp_path)
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    finals = load_secrets_plaintext(secrets_path(tmp_path))
    assert "real-public-key" in finals["HOMELAB_SSH_PUBLIC_KEY"]


def test_rotate_actually_rotates_generated_passwords(tmp_path: Path):
    _bootstrap_secrets(tmp_path)
    # REDIS_PASSWORD has a reconcile policy; generic rotation must produce a
    # new value (not the old one, not blank) before the service hook applies it.
    save_secrets_plaintext(
        {"REDIS_PASSWORD": "old-pw-12345678"},
        secrets_path(tmp_path),
    )
    rotate_secrets(tmp_path, specific=["REDIS_PASSWORD"])
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    finals = load_secrets_plaintext(secrets_path(tmp_path))
    assert finals["REDIS_PASSWORD"] != "old-pw-12345678"
    assert len(finals["REDIS_PASSWORD"]) >= 16


def test_rotate_all_preserves_fixed_defaults(tmp_path: Path):
    _bootstrap_secrets(tmp_path)
    save_secrets_plaintext({"QBITTORRENT_USER": "admin"}, secrets_path(tmp_path))

    rotate_secrets(tmp_path)

    from toolkit.core.secrets.secrets import load_secrets_plaintext

    assert load_secrets_plaintext(secrets_path(tmp_path))["QBITTORRENT_USER"] == "admin"


def test_explicit_persistent_secret_rotation_is_rejected(tmp_path: Path):
    _bootstrap_secrets(tmp_path)

    import pytest

    with pytest.raises(ValueError, match="service-owned migration"):
        rotate_secrets(tmp_path, specific=["AUTHELIA_STORAGE_KEY"])
