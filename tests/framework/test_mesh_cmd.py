from toolkit.cli.mesh_cmd import _extract_registration_key


def test_extract_registration_key_from_current_tailscale_path_url() -> None:
    key = "hskey-authreq-example_123"

    assert _extract_registration_key(f"https://vpn.example.com/register/{key}") == key


def test_extract_registration_key_from_legacy_query_url() -> None:
    key = "hskey-authreq-example_456"

    assert _extract_registration_key(f"https://vpn.example.com/register?key={key}") == key


def test_extract_registration_key_returns_empty_for_unrelated_output() -> None:
    assert _extract_registration_key("tailscale is already connected") == ""
