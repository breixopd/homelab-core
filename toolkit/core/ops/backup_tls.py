"""Generate and validate the private TLS identity for the Kopia repository server."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True, slots=True)
class KopiaServerCertificate:
    cert_path: Path
    key_path: Path
    fingerprint: str


def _certificate_matches(cert_path: Path, key_path: Path, address: ipaddress.IPv4Address) -> x509.Certificate | None:
    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        public = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        expected_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if (
            address not in san.get_values_for_type(x509.IPAddress)
            or "kopia" not in san.get_values_for_type(x509.DNSName)
            or public != expected_public
            or certificate.not_valid_after_utc < datetime.now(UTC) + timedelta(days=30)
        ):
            return None
        return certificate
    except (OSError, ValueError, TypeError, x509.ExtensionNotFound):
        return None


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    temporary.replace(path)
    path.chmod(mode)


def ensure_kopia_server_certificate(root: Path, private_ip: str) -> KopiaServerCertificate:
    """Return a stable self-signed certificate, rotating it when its private endpoint changes."""
    address = ipaddress.ip_address(private_ip)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("Kopia server endpoint must be an IPv4 address")
    directory = root / "generated" / "kopia" / "tls"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    certificate = _certificate_matches(cert_path, key_path, address)
    if certificate is None:
        private_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homelab-kopia")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("kopia"), x509.IPAddress(address)]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256())
        )
        _atomic_write(
            key_path,
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            0o600,
        )
        _atomic_write(cert_path, certificate.public_bytes(serialization.Encoding.PEM), 0o644)
    return KopiaServerCertificate(cert_path, key_path, certificate.fingerprint(hashes.SHA256()).hex())
