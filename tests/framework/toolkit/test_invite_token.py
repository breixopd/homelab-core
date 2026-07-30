from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.identity.invite_token import (
    begin_invite_activation,
    complete_invite_activation,
    create_invite_token,
    invite_csrf_token,
    peek_invite_token,
    validate_invite_csrf,
)


@pytest.fixture
def secrets():
    return {"INVITE_TOKEN_SECRET": "test-secret-key-for-invite-tokens-0123456789"}


@pytest.fixture
def mock_redis():
    store: dict[str, str] = {}
    client = MagicMock()

    def setex(key, ttl, value):
        store[key] = value

    def get(key):
        return store.get(key)

    def getdel(key):
        return store.pop(key, None)

    def eval_script(script, _numkeys, *args):
        if "INVITE_ISSUE" in script:
            invite_key, subject_key, delivery_key, jti, _ttl, value, invite_prefix, delivery_record = args
            if delivery_key and delivery_key in store:
                return f"CACHED:{store[delivery_key]}"
            previous = store.get(subject_key)
            if previous:
                store.pop(f"{invite_prefix}{previous}", None)
            store[invite_key] = value
            store[subject_key] = jti
            if delivery_key:
                store[delivery_key] = delivery_record
            return "ISSUED"
        if "INVITE_ACTIVATION_BEGIN" in script:
            invite_key, subject_key, activation_key, jti, activation_id, _ttl = args
            state = store.get(activation_key)
            if state:
                return f"STATE:{state}"
            value = store.get(invite_key)
            if value is None or store.get(subject_key) != jti:
                return None
            store[activation_key] = f"ACTIVATING:{activation_id}"
            store.pop(invite_key, None)
            store.pop(subject_key, None)
            return value
        if "INVITE_ACTIVATION_FINISH" in script:
            activation_key = args[0]
            activation_id, state, _ttl = args[1:]
            if store.get(activation_key) != f"ACTIVATING:{activation_id}":
                return 0
            store[activation_key] = state
            return 1
        invite_key, subject_key, jti = args
        value = store.get(invite_key)
        if value is None or store.get(subject_key) != jti:
            return None
        if "INVITE_CONSUME" in script:
            store.pop(invite_key, None)
            store.pop(subject_key, None)
        return value

    client.setex = setex
    client.get = get
    client.getdel = getdel
    client.eval = eval_script
    client._store = store
    return client


def test_invite_activation_consumes_before_mutation_and_records_terminal_result(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media", "homelab-cloud"],
        )
        peeked = peek_invite_token(secrets, token)
        assert peeked is not None
        assert peeked["email"] == "family@example.com"
        assert peeked["user_id"] == "family"

        activation = begin_invite_activation(secrets, token)
        assert activation.state == "acquired"
        assert activation.payload == peeked
        assert peek_invite_token(secrets, token) is None
        assert begin_invite_activation(secrets, token).state == "activating"
        assert complete_invite_activation(
            secrets,
            token,
            activation.activation_id,
            succeeded=True,
        )
        assert begin_invite_activation(secrets, token).state == "succeeded"


def test_invite_activation_failure_is_terminal_and_owner_bound(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
        )
        activation = begin_invite_activation(secrets, token)

        assert activation.state == "acquired"
        assert begin_invite_activation(secrets, token).state == "activating"
        assert not complete_invite_activation(secrets, token, "wrong-claim", succeeded=False)
        assert complete_invite_activation(
            secrets,
            token,
            activation.activation_id,
            succeeded=False,
        )
        assert peek_invite_token(secrets, token) is None
        assert begin_invite_activation(secrets, token).state == "failed"


def test_invite_token_contains_only_opaque_identifiers(secrets, mock_redis):
    from toolkit.core.identity.invite_token import _serializer

    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
        )

    envelope = _serializer(secrets["INVITE_TOKEN_SECRET"]).loads(token)
    assert set(envelope) == {"v", "jti", "subject", "payload_sha256"}
    assert "family@example.com" not in str(envelope)
    assert "homelab-media" not in str(envelope)


def test_new_invite_revokes_previous_invite_for_same_user(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        first = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
        )
        second = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media", "homelab-cloud"],
        )

        assert peek_invite_token(secrets, first) is None
        assert begin_invite_activation(secrets, first).state == "invalid"
        assert peek_invite_token(secrets, second) is not None


def test_retry_with_same_issuance_id_reuses_exact_invite_token(secrets, mock_redis) -> None:
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        first = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
            issuance_id="identity-job-1234",
        )
        second = create_invite_token(
            secrets,
            email="family@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
            issuance_id="identity-job-1234",
        )

    assert second == first


def test_new_invite_revokes_previous_email_for_same_user_id(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        first = create_invite_token(
            secrets,
            email="old@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
        )
        create_invite_token(
            secrets,
            email="new@example.com",
            user_id="family",
            display_name="Family",
            groups=["homelab-media"],
        )

        assert peek_invite_token(secrets, first) is None


def test_invite_token_rejects_weak_signing_secret(mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        with pytest.raises(RuntimeError, match="at least 32"):
            create_invite_token(
                {"INVITE_TOKEN_SECRET": "too-short"},
                email="family@example.com",
                user_id="family",
                display_name="Family",
                groups=["homelab-media"],
            )


def test_activation_csrf_is_bound_to_invite_token(secrets):
    csrf = invite_csrf_token(secrets, "opaque-invite-token")

    assert validate_invite_csrf(secrets, "opaque-invite-token", csrf)
    assert not validate_invite_csrf(secrets, "different-invite-token", csrf)
    assert not validate_invite_csrf(secrets, "opaque-invite-token", "invalid")


def test_redis_client_has_bounded_network_timeouts(monkeypatch):
    from toolkit.core.identity import invite_token

    redis_factory = MagicMock()
    monkeypatch.setitem(os.environ, "HOMELAB_REDIS_HOST", "cache.internal")
    monkeypatch.setitem(os.environ, "HOMELAB_REDIS_PORT", "6380")
    monkeypatch.setitem(os.environ, "REDIS_PASSWORD", "redis-secret")

    with patch("redis.Redis", redis_factory):
        invite_token._redis_client()

    redis_factory.assert_called_once_with(
        host="cache.internal",
        port=6380,
        password="redis-secret",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=3,
        health_check_interval=30,
    )


@pytest.mark.parametrize("user_id", ["", "admin", "ldap-bind", "UPPER", "spaces are invalid"])
def test_invite_token_rejects_unsafe_user_ids(secrets, mock_redis, user_id):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        with pytest.raises(ValueError, match="payload"):
            create_invite_token(
                secrets,
                email="family@example.com",
                user_id=user_id,
                display_name="Family",
                groups=["homelab-media"],
            )


def test_invite_token_rejects_bad_signature(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="a@b.com",
            user_id="a",
            display_name=None,
            groups=["homelab-media"],
        )
    assert peek_invite_token(secrets, token + "x") is None


def test_invite_token_rejects_malformed_redis_payload(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="a@b.com",
            user_id="a",
            display_name=None,
            groups=["homelab-media"],
        )
        key = next(iter(mock_redis._store))
        mock_redis._store[key] = "not-json"

        assert peek_invite_token(secrets, token) is None
        assert begin_invite_activation(secrets, token).state == "failed"


def test_invite_token_rejects_redis_identity_rewrite(secrets, mock_redis):
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        token = create_invite_token(
            secrets,
            email="a@b.com",
            user_id="a",
            display_name="A",
            groups=["homelab-media"],
        )
        key = next(iter(mock_redis._store))
        payload = json.loads(mock_redis._store[key])
        payload["user_id"] = "admin"
        mock_redis._store[key] = json.dumps(payload)

        assert peek_invite_token(secrets, token) is None
        assert begin_invite_activation(secrets, token).state == "failed"
