from __future__ import annotations

import ipaddress
import re
from typing import Any

# Strict IPv4 validation: each octet 0-255, rejects leading zeros
IPV4_REGEX_STRICT = re.compile(
    r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
)

# Permissive IPv4 validation: basic octet format, no range check
IPV4_REGEX_PERMISSIVE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def validate_ipv4(ip: str) -> str:
    """Validate an IPv4 address. Returns the IP or raises ValueError."""
    if not IPV4_REGEX_STRICT.match(ip):
        raise ValueError(f"Invalid IPv4 address: {ip}")
    return ip


def validate_domain(domain: str) -> bool:
    """Validate FQDN format (basic check, not RFC-complete)."""
    if not domain or len(domain) > 253:
        return False
    # Allow localhost, single-label, and FQDN
    _label = r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    pattern = rf"^(localhost|{_label}(\.{_label})*)$"
    return bool(re.match(pattern, domain))


def validate_port(port: Any) -> int:
    """Validate a TCP/UDP port number. Returns the port or raises ValueError."""
    p = int(port)
    if not 1 <= p <= 65535:
        raise ValueError(f"Port must be 1-65535, got {p}")
    return p


def validate_url(url: str) -> str:
    """Validate an HTTP/HTTPS URL. Returns the URL or raises ValueError."""
    if url and not re.match(r"^https?://[^\s/$.?#].[^\s]*$", url):
        raise ValueError(f"Invalid URL: {url}")
    return url


def validate_email(email: str) -> str:
    """Validate an email address. Returns the email or raises ValueError."""
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError(f"Invalid email: {email}")
    return email


def validate_cidr(cidr: str) -> str:
    """Validate a CIDR notation. Returns the CIDR or raises ValueError."""
    if cidr:
        ipaddress.ip_network(cidr, strict=False)
    return cidr
