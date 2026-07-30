"""Security regressions for Headscale bootstrap output."""

from __future__ import annotations

from toolkit.services.headscale.bootstrap import _unmasked_preauth_key, bootstrap_headscale_preauth


def test_listed_preauth_key_rejects_headscale_masked_secret() -> None:
    assert _unmasked_preauth_key("hskey-auth-prefix-***") is None
    assert _unmasked_preauth_key("hskey-auth-prefix-full-secret") == "hskey-auth-prefix-full-secret"


def test_bootstrap_preauth_log_never_echoes_bearer_key(monkeypatch) -> None:
    key = "hskey-auth-this-is-a-real-bearer-secret"
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.headscale_preauth_key",
        lambda **_kwargs: key,
    )

    logs = bootstrap_headscale_preauth(tags=["tag:fleet-external"])

    assert logs == ["Headscale: preauth key ready"]
    assert key not in "\n".join(logs)
    assert key[:24] not in "\n".join(logs)


def test_bootstrap_preauth_failure_log_contains_no_credential_material(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.headscale_preauth_key",
        lambda **_kwargs: None,
    )

    logs = bootstrap_headscale_preauth()

    assert logs == ["Headscale: preauth key create failed"]
