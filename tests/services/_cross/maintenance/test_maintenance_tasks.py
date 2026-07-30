"""Automated maintenance tasks and policy validation.

All pure/unit-testable: subprocess calls are mocked; time.sleep is a no-op
under the autouse conftest stub. These functions are the 'verdict/remedy'
pieces run_maintenance() orchestrates.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError
from toolkit.core.ops.maintenance_tasks import (
    MaintenanceConfig,
    check_cert_expiries,
    scan_image_updates,
)


def test_maintenance_config_defaults():
    cfg = MaintenanceConfig()
    assert cfg.enabled is True
    assert cfg.daily_at == "03:00"
    assert cfg.cert_warning_days == 14
    assert cfg.image_update_scan is True


def test_maintenance_config_roundtrips():
    cfg = MaintenanceConfig(enabled=False, daily_at="04:30", cert_warning_days=30)
    assert cfg.enabled is False
    assert cfg.daily_at == "04:30"
    assert cfg.cert_warning_days == 30


@pytest.mark.parametrize("value", ["3:00", "24:00", "03:60", "0 3 * * *", "03:00\nOnBootSec=1s"])
def test_maintenance_daily_time_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError):
        MaintenanceConfig(daily_at=value)


@pytest.mark.parametrize("field", ["vacuum_dbs", "container_log_truncate_mb"])
def test_maintenance_rejects_removed_mutating_chore_options(field: str) -> None:
    with pytest.raises(ValidationError):
        MaintenanceConfig.model_validate({field: True if field == "vacuum_dbs" else 100})


# --- check_cert_expiries ---------------------------------------------------


def test_check_cert_expiries_warns_under_threshold():
    # A cert expiring in 5 days (< 14-day warning) → flag.
    logs = check_cert_expiries(
        domain="example.com",
        days_left=5,
        warning_days=14,
    )
    assert any("expir" in ln.lower() for ln in logs)


def test_check_cert_expiries_silent_when_far():
    logs = check_cert_expiries(
        domain="example.com",
        days_left=90,
        warning_days=14,
    )
    # No warning when plenty of time left.
    assert not any("expir" in ln.lower() and "warn" in ln.lower() for ln in logs) or logs == []


def test_check_cert_expiries_expired():
    logs = check_cert_expiries(domain="example.com", days_left=-2, warning_days=14)
    assert any("expired" in ln.lower() for ln in logs)


# --- scan_image_updates ----------------------------------------------------


def test_scan_image_updates_returns_proposals():
    # Returns a list of (service, current, latest) tuples for unapproved updates.
    with patch(
        "toolkit.core.ops.maintenance_tasks._fetch_image_versions",
        return_value={
            "grafana": {"current": "10.0.0", "latest": "10.1.0"},
            "postgres": {"current": "16.2", "latest": "16.2"},  # up to date
        },
    ):
        proposals = scan_image_updates()
    assert len(proposals) == 1
    assert proposals[0]["service"] == "grafana"
    assert proposals[0]["current"] == "10.0.0"
    assert proposals[0]["latest"] == "10.1.0"


def test_scan_image_updates_disabled():
    cfg = MaintenanceConfig(image_update_scan=False)
    with patch("toolkit.core.ops.maintenance_tasks._fetch_image_versions") as m:
        proposals = scan_image_updates(cfg=cfg)
    assert proposals == []
    assert m.call_count == 0
