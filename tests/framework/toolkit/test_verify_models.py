from __future__ import annotations

from toolkit.core.verify.models import HookVerifyResult, VerifyCheck


def test_retryable_failures_excludes_nonretryable_failures() -> None:
    result = HookVerifyResult(
        checks=[
            VerifyCheck("music-sync", "api_status", False, "manual authorization", retryable=False),
            VerifyCheck("caddy", "health", False, "temporarily unavailable"),
            VerifyCheck("ldap", "bind", True, "ok"),
        ]
    )

    assert result.retryable_failures == [result.checks[1]]


def test_retryable_failures_is_empty_when_all_failures_are_manual() -> None:
    result = HookVerifyResult(
        checks=[VerifyCheck("music-sync", "api_status", False, "manual authorization", retryable=False)]
    )

    assert result.retryable_failures == []
