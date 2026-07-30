from __future__ import annotations

import pytest
from toolkit.core.config.validators import validate_domain, validate_ipv4


class TestValidateIPv4:
    def test_valid_ip(self):
        assert validate_ipv4("192.168.1.1") == "192.168.1.1"

    def test_valid_localhost_ip(self):
        assert validate_ipv4("127.0.0.1") == "127.0.0.1"

    def test_valid_broadcast(self):
        assert validate_ipv4("255.255.255.255") == "255.255.255.255"

    def test_valid_zero_ip(self):
        assert validate_ipv4("0.0.0.0") == "0.0.0.0"

    def test_invalid_ip_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("not-an-ip")

    def test_invalid_ip_with_text(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("192.168.1.abc")

    def test_invalid_ip_empty_string(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("")

    def test_invalid_ip_too_many_octets(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("1.2.3.4.5")

    def test_invalid_ip_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("999.999.999.999")

    def test_invalid_ip_with_negative(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            validate_ipv4("-1.2.3.4")


class TestValidateDomain:
    def test_valid_fqdn(self):
        assert validate_domain("example.com") is True

    def test_valid_subdomain(self):
        assert validate_domain("subdomain.example.com") is True

    def test_valid_localhost(self):
        assert validate_domain("localhost") is True

    def test_valid_multi_level(self):
        assert validate_domain("a.b.c.example.com") is True

    def test_invalid_domain_with_spaces(self):
        assert validate_domain("not a domain") is False

    def test_invalid_empty_string(self):
        assert validate_domain("") is False

    def test_invalid_domain_too_long(self):
        assert validate_domain("a" * 254) is False

    def test_invalid_starts_with_hyphen(self):
        assert validate_domain("-example.com") is False

    def test_invalid_ends_with_dot(self):
        assert validate_domain("example.") is False

    def test_valid_domain_with_hyphen(self):
        assert validate_domain("my-example.com") is True
