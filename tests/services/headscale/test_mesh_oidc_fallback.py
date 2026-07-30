from __future__ import annotations

from toolkit.services.headscale.bootstrap import headscale_oidc_cli_fallback


def test_headscale_oidc_cli_fallback_detects_after_latest_start():
    logs = """
2026-06-18T16:41:49+02:00 INF Starting Headscale
2026-06-18T16:41:49+02:00 WRN failed to set up OIDC provider, falling back to CLI based authentication
"""
    assert headscale_oidc_cli_fallback(logs)


def test_headscale_oidc_cli_fallback_ignores_old_fallback_before_restart():
    logs = """
2026-06-18T16:41:49+02:00 WRN failed to set up OIDC provider, falling back to CLI based authentication
2026-06-18T17:00:00+02:00 INF Starting Headscale
2026-06-18T17:00:01+02:00 INF OIDC provider configured
"""
    assert not headscale_oidc_cli_fallback(logs)
