from toolkit.controller.contracts import JobKind, JobRequest, ServiceVerifyOperation
from toolkit.controller.service_management_api import aggregate_verification_status
from toolkit.core.verify.models import VerifyCheck, VerifyStatus


def test_service_verify_contract_is_typed_and_framework_defaults_off() -> None:
    operation = ServiceVerifyOperation(service="grafana")
    request = JobRequest(idempotency_key="service-verify-grafana", operation=operation)
    assert request.kind is JobKind.SERVICE_VERIFY
    assert operation.include_framework is False


def test_verify_check_bounds_and_credential_redaction() -> None:
    check = VerifyCheck("grafana", "health", False, "token=super-secret " + "x" * 500)
    assert check.status is VerifyStatus.FAIL
    assert len(check.detail) == 200
    assert "super-secret" not in check.detail
    assert "[REDACTED]" in check.detail


def test_service_status_precedence_and_all_not_applicable() -> None:
    assert aggregate_verification_status(["pass", "degraded", "not_ready"]) == "not_ready"
    assert aggregate_verification_status(["pass", "fail", "not_ready"]) == "fail"
    assert aggregate_verification_status(["not_applicable", "not_applicable"]) == "not_applicable"
    assert aggregate_verification_status([]) == "not_applicable"
