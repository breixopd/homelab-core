from __future__ import annotations

from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtensionOID
from toolkit.core.ops.backup_tls import ensure_kopia_server_certificate


def test_kopia_server_certificate_is_stable_and_scoped_to_private_endpoint(tmp_path: Path) -> None:
    first = ensure_kopia_server_certificate(tmp_path, "10.10.10.10")
    second = ensure_kopia_server_certificate(tmp_path, "10.10.10.10")

    assert first == second
    assert len(first.fingerprint) == 64
    assert first.cert_path == tmp_path / "generated/kopia/tls/server.crt"
    assert first.key_path == tmp_path / "generated/kopia/tls/server.key"
    assert first.cert_path.stat().st_mode & 0o777 == 0o644
    assert first.key_path.stat().st_mode & 0o777 == 0o600
    certificate = x509.load_pem_x509_certificate(first.cert_path.read_bytes())
    san = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert "10.10.10.10" in {str(address) for address in san.get_values_for_type(x509.IPAddress)}
    assert "kopia" in san.get_values_for_type(x509.DNSName)


def test_kopia_server_certificate_rotates_when_private_endpoint_changes(tmp_path: Path) -> None:
    first = ensure_kopia_server_certificate(tmp_path, "10.10.10.10")
    second = ensure_kopia_server_certificate(tmp_path, "10.10.10.11")

    assert first.fingerprint != second.fingerprint
