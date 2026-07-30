from __future__ import annotations

import pytest
from toolkit.core.verify.models import (
    HookVerifyResult,
    VerifyCheck,
    VerifyStatus,
    format_verify_report,
)


def test_explicit_states_only_pass_is_counted_as_successful() -> None:
    checks = [
        VerifyCheck("svc", "pass", True, status=VerifyStatus.PASS),
        VerifyCheck("svc", "fail", False, status=VerifyStatus.FAIL),
        VerifyCheck("svc", "optional", True, status=VerifyStatus.NOT_APPLICABLE),
        VerifyCheck("svc", "degraded", False, status=VerifyStatus.DEGRADED),
        VerifyCheck("svc", "starting", False, status=VerifyStatus.NOT_READY),
    ]
    result = HookVerifyResult(checks)

    assert [check.passed for check in checks] == [True, False, True, False, False]
    assert result.all_passed is False
    assert result.passed_count == 1
    assert result.failed_count == 3
    assert result.failed_checks == [checks[1], checks[3], checks[4]]
    assert "1/4 checks passed" in result.summary


def test_not_applicable_is_excluded_from_readiness_denominator() -> None:
    result = HookVerifyResult(
        [
            VerifyCheck("svc", "health", True, status=VerifyStatus.PASS),
            VerifyCheck("svc", "optional", True, status=VerifyStatus.NOT_APPLICABLE),
        ]
    )

    assert result.all_passed is True
    assert result.passed_count == 1
    assert result.failed_count == 0
    assert result.summary == "1/1 checks passed"


def test_legacy_skipped_detail_is_neutral_not_counted_pass() -> None:
    check = VerifyCheck("svc", "probe", True, "skipped (localhost)")

    assert check.status is VerifyStatus.NOT_APPLICABLE
    assert check.passed is True


def test_security_posture_detail_is_not_treated_as_skipped() -> None:
    check = VerifyCheck("roundcube", "installer", True, "installer disabled")

    assert check.status is VerifyStatus.PASS
    assert check.passed is True


def test_explicit_status_rejects_contradictory_legacy_boolean() -> None:
    with pytest.raises(ValueError, match="PASS status requires"):
        VerifyCheck("svc", "probe", False, status=VerifyStatus.PASS)
    with pytest.raises(ValueError, match="NOT_APPLICABLE status requires"):
        VerifyCheck("svc", "probe", False, status=VerifyStatus.NOT_APPLICABLE)
    with pytest.raises(ValueError, match="not_ready status requires"):
        VerifyCheck("svc", "probe", True, status=VerifyStatus.NOT_READY)


def test_report_distinguishes_non_pass_states() -> None:
    result = HookVerifyResult(
        [
            VerifyCheck("svc", "optional", True, status=VerifyStatus.NOT_APPLICABLE),
            VerifyCheck("svc", "ready", False, status=VerifyStatus.NOT_READY),
        ]
    )

    report = format_verify_report(result)
    assert "○ svc.optional" in report
    assert "… svc.ready" in report


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
