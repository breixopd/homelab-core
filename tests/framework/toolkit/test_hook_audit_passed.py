"""A single WARNING must NOT abort the deploy.

Today HookAuditSummary.passed returns False if ANY warning exists, which flips
all_success=False in deploy_workflow — so a soft "not ready yet" WARNING kills
the whole deploy even though all containers are up. WARNINGs should be visible
(in the manual-steps section) but not abort; only CRITICAL should abort.
"""

from __future__ import annotations

from toolkit.core.deploy.hook_audit import HookAuditSummary, save_last_hooks_report, strict_hooks_passed


def test_passed_with_no_issues():
    summary = HookAuditSummary(vm="infra", ok=5)
    assert summary.passed is True


def test_passed_false_on_critical():
    summary = HookAuditSummary(vm="infra", critical=1, ok=4)
    assert summary.passed is False


def test_passed_true_with_only_warnings():
    """A WARNING (soft failure, e.g. 'not ready yet') must NOT abort the deploy.
    Only CRITICAL (hook error, bootstrap failure) should flip passed=False."""
    summary = HookAuditSummary(vm="infra", warning=2, ok=3)
    assert summary.passed is True, (
        "WARNINGs must not abort the deploy — they route to manual-steps. "
        "Only CRITICAL should flip passed=False (enables hands-off deploys)."
    )


def test_summary_tracks_warnings_for_manual_steps():
    """Even though passed=True, the warning count must be preserved so the
    manual-steps block can surface them."""
    summary = HookAuditSummary(vm="infra", warning=2, ok=3)
    assert summary.warning == 2
    assert summary.passed is True


def test_strict_passed_requires_zero_warnings():
    assert HookAuditSummary(vm="infra", ok=3).strict_passed is True
    assert HookAuditSummary(vm="infra", warning=1, ok=3).strict_passed is False


def test_persisted_strict_audit_requires_a_report(tmp_path):
    passed, detail = strict_hooks_passed(tmp_path)
    assert passed is False
    assert "no persisted" in detail


def test_persisted_strict_audit_accepts_clean_nodes(tmp_path):
    save_last_hooks_report(tmp_path, {"infra": HookAuditSummary(vm="infra", ok=3)})

    passed, detail = strict_hooks_passed(tmp_path)

    assert passed is True
    assert "1 node" in detail


def test_persisted_strict_audit_rejects_warnings(tmp_path):
    save_last_hooks_report(tmp_path, {"infra": HookAuditSummary(vm="infra", warning=1, ok=2)})

    passed, detail = strict_hooks_passed(tmp_path)

    assert passed is False
    assert "1 warning" in detail
