from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from toolkit.controller.read_models import (
    BootstrapDesiredState,
    BootstrapInitializeRequest,
)
from toolkit.controller.store import BootstrapCapabilityError, ControllerStore


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _desired_state() -> BootstrapDesiredState:
    return BootstrapDesiredState(
        domain="home.example.com",
        email="operator@example.com",
        timezone="Europe/Madrid",
        proxmox_api_url="https://192.0.2.10:8006",
        proxmox_node="pve",
        proxmox_storage="local-zfs",
        service_settings={
            "media-library": {"server": "jellyfin"},
            "gluetun": {"enabled": True},
        },
    )


def _replace_secret(token: str) -> str:
    token_id, _, _secret = token.partition(".")
    return f"{token_id}.not-the-issued-secret"


def test_bootstrap_desired_state_rejects_credentials() -> None:
    values = _desired_state().model_dump()
    values["cloudflare_api_token"] = "must-not-live-here"

    with pytest.raises(ValidationError):
        BootstrapDesiredState.model_validate(values)

    request = BootstrapInitializeRequest(
        session_token="00000000-0000-4000-8000-000000000000.session-secret-value",
        desired_state=_desired_state(),
        credential_values={
            "CLOUDFLARE_API_TOKEN": "cloudflare-secret",
            "PROXMOX_API_TOKEN_SECRET": "proxmox-secret",
        },
    )
    assert set(request.credential_values) == {
        "CLOUDFLARE_API_TOKEN",
        "PROXMOX_API_TOKEN_SECRET",
    }
    assert "cloudflare-secret" not in repr(request)


def test_bootstrap_desired_state_requires_owner_email() -> None:
    values = _desired_state().model_dump()
    values["email"] = ""

    with pytest.raises(ValidationError):
        BootstrapDesiredState.model_validate(values)


def test_bootstrap_desired_state_rejects_named_service_fields() -> None:
    values = _desired_state().model_dump()
    values["media_server"] = "jellyfin"

    with pytest.raises(ValidationError):
        BootstrapDesiredState.model_validate(values)


def test_capability_is_hashed_at_rest_and_exchanges_once(tmp_path: Path) -> None:
    clock = MutableClock()
    store = ControllerStore(tmp_path / "controller.db", clock=clock)

    capability = store.issue_bootstrap_capability(
        principal="local:operator",
        ttl=timedelta(minutes=10),
    )

    with closing(sqlite3.connect(store.path)) as connection, connection:
        row = connection.execute(
            "SELECT token_hash, exchanged_at, failed_attempts FROM bootstrap_capabilities"
        ).fetchone()
    assert row is not None
    assert row[0] != capability.token
    assert row[1] is None
    assert row[2] == 0
    assert capability.token.encode() not in store.path.read_bytes()

    grant = store.exchange_bootstrap_capability(
        capability.token,
        ttl=timedelta(minutes=5),
    )
    assert grant.expires_at == clock.now + timedelta(minutes=5)
    assert grant.session_token.encode() not in store.path.read_bytes()

    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.exchange_bootstrap_capability(capability.token, ttl=timedelta(minutes=5))


def test_issuing_capability_revokes_prior_capability_and_session(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    first = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))
    first_grant = store.exchange_bootstrap_capability(first.token, ttl=timedelta(minutes=5))

    second = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))

    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.validate_bootstrap_grant(first_grant.session_token)
    second_grant = store.exchange_bootstrap_capability(second.token, ttl=timedelta(minutes=5))
    assert store.validate_bootstrap_grant(second_grant.session_token).expires_at == second_grant.expires_at


def test_capability_and_grant_expire(tmp_path: Path) -> None:
    clock = MutableClock()
    store = ControllerStore(tmp_path / "controller.db", clock=clock)
    capability = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=2))
    clock.now += timedelta(minutes=3)

    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.exchange_bootstrap_capability(capability.token, ttl=timedelta(minutes=1))

    fresh = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=5))
    grant = store.exchange_bootstrap_capability(fresh.token, ttl=timedelta(minutes=1))
    clock.now += timedelta(minutes=2)
    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.validate_bootstrap_grant(grant.session_token)


def test_failed_attempts_revoke_capability_and_grant(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    capability = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))

    for _ in range(5):
        with pytest.raises(BootstrapCapabilityError, match="invalid"):
            store.exchange_bootstrap_capability(_replace_secret(capability.token), ttl=timedelta(minutes=5))
    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.exchange_bootstrap_capability(capability.token, ttl=timedelta(minutes=5))

    fresh = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))
    grant = store.exchange_bootstrap_capability(fresh.token, ttl=timedelta(minutes=5))
    for _ in range(5):
        with pytest.raises(BootstrapCapabilityError, match="invalid"):
            store.validate_bootstrap_grant(_replace_secret(grant.session_token))
    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.validate_bootstrap_grant(grant.session_token)


def test_grant_is_consumed_once_after_success(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    capability = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))
    grant = store.exchange_bootstrap_capability(capability.token, ttl=timedelta(minutes=5))

    assert store.bootstrap_access_state() == (False, True)
    store.consume_bootstrap_grant(grant.session_token, principal="mtls:homelab-ui")
    assert store.bootstrap_access_state() == (False, False)

    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.consume_bootstrap_grant(grant.session_token, principal="mtls:homelab-ui")


@pytest.mark.parametrize(
    "ttl",
    [timedelta(0), timedelta(seconds=-1), timedelta(minutes=16)],
)
def test_capability_ttl_is_bounded(tmp_path: Path, ttl: timedelta) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    with pytest.raises(BootstrapCapabilityError, match="lifetime"):
        store.issue_bootstrap_capability(principal="local:operator", ttl=ttl)


@pytest.mark.parametrize(
    "ttl",
    [timedelta(0), timedelta(seconds=-1), timedelta(minutes=16)],
)
def test_session_ttl_is_bounded(tmp_path: Path, ttl: timedelta) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    capability = store.issue_bootstrap_capability(principal="local:operator", ttl=timedelta(minutes=10))
    with pytest.raises(BootstrapCapabilityError, match="lifetime"):
        store.exchange_bootstrap_capability(capability.token, ttl=ttl)
