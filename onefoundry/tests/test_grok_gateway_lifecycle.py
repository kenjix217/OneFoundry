"""Grok account gateway lifecycle without a database or provider egress."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import Engine

from onefoundry.grok_account import (
    DeviceChallenge,
    GrokConnectionDenied,
    GrokConnectionError,
    GrokConnectionExpired,
    GrokConnectionPending,
    GrokConnectionUnknown,
    GrokCredentialTooLarge,
    decode_state,
    encode_state,
)
from onefoundry.model_gateway import ModelGateway, make_model_router
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings


class _Row:
    def __init__(self, values: tuple[object, ...]) -> None:
        self.values = values

    def one_or_none(self) -> tuple[object, ...]:
        return self.values


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine

    def execute(self, statement: object, params: object) -> _Row:
        sql = str(statement)
        self.engine.statements.append(sql)
        if sql.startswith("SELECT credential_ready,catalog_valid,credential_revision"):
            return _Row((self.engine.ready, self.engine.catalog_valid, self.engine.revision))
        if sql.startswith("SELECT credential_revision,credential_ready"):
            return _Row((self.engine.revision, self.engine.ready))
        if sql.startswith("SELECT auth_kind,credential_ready"):
            return _Row(("account_runtime", self.engine.ready, self.engine.revision))
        if sql.startswith("SELECT credential_ready"):
            return _Row((self.engine.ready,))
        if sql.startswith("UPDATE model_accounts SET credential_ready=true"):
            self.engine.ready = True
            if "catalog_valid=false" in sql:
                self.engine.catalog_valid = False
        if sql.startswith("UPDATE model_accounts SET credential_ready=false"):
            self.engine.ready = False
            self.engine.catalog_valid = False
        if sql.startswith("UPDATE model_accounts SET catalog_valid=false"):
            self.engine.catalog_valid = False
        if sql.startswith("UPDATE model_accounts") and "credential_revision" in sql:
            if isinstance(params, dict) and "revision" in params:
                self.engine.revision = int(params["revision"])
            else:
                self.engine.revision += 1
        return _Row(())


class _Engine:
    def __init__(self) -> None:
        self.ready = False
        self.catalog_valid = True
        self.revision = 0
        self.lock = threading.RLock()
        self.statements: list[str] = []

    @contextmanager
    def begin(self) -> Iterator[_Connection]:
        with self.lock:
            snapshot = (self.ready, self.catalog_valid, self.revision)
            try:
                yield _Connection(self)
            except BaseException:
                self.ready, self.catalog_valid, self.revision = snapshot
                raise


def _gateway(tmp_path: Path) -> tuple[ModelGateway, _Engine, UUID, UUID]:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    engine = _Engine()
    project_id = uuid4()
    account_id = uuid4()
    gateway = ModelGateway(
        cast(Engine, engine),
        "operator",
        ModelSecretStore(str(root)),
        Settings(db_password="test", xai_oauth_client_id="fixture-client"),
    )
    object.__setattr__(
        gateway,
        "_account",
        lambda project, account: (
            gateway._redact(
                {
                    "id": account_id,
                    "provider": "xai",
                    "auth_kind": "account_runtime",
                    "credential_ready": engine.ready,
                    "credential_revision": engine.revision,
                    "catalog_valid": engine.catalog_valid,
                }
            )
            if project == project_id and account == account_id
            else None
        ),
    )
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "pending",
                "device_code": "private-device",
                "user_code": "GROK-CODE",
                "verification_url": "https://auth.x.ai/device",
                "expires_at": int(time.time()) + 600,
                "interval_seconds": 5,
                "next_poll_at": 0,
            }
        ),
    )
    return gateway, engine, project_id, account_id


def test_finish_serializes_exchange_and_recovers_saved_credential(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    exchanged = {
        "kind": "connected",
        "access_token": "private-access",
        "refresh_token": "private-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    calls = 0

    def exchange(*args: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert any("FOR UPDATE" in sql for sql in engine.statements)
        assert decode_state(gateway.secrets.read(account_id))["kind"] == "exchanging"
        return exchanged

    with patch("onefoundry.model_gateway.finish_grok_device_login", side_effect=exchange):
        first = gateway.finish_grok(project_id, account_id)
        second = gateway.finish_grok(project_id, account_id)
    assert first is not None and second is not None
    assert calls == 1
    assert engine.ready
    assert decode_state(gateway.secrets.read(account_id)) == {**exchanged, "credential_revision": 1}


def test_finish_persists_poll_interval_and_blocks_early_retry(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    with patch(
        "onefoundry.model_gateway.finish_grok_device_login",
        side_effect=GrokConnectionPending(interval_seconds=10),
    ) as exchange:
        with pytest.raises(GrokConnectionPending) as first:
            gateway.finish_grok(project_id, account_id)
        assert first.value.interval_seconds == 10
        saved = decode_state(gateway.secrets.read(account_id))
        assert saved["kind"] == "pending" and saved["interval_seconds"] == 10
        assert saved["next_poll_at"] > time.time()
        with pytest.raises(GrokConnectionPending):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()


def test_finish_records_unknown_exchange_and_never_replays_device_grant(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    with patch(
        "onefoundry.model_gateway.finish_grok_device_login",
        side_effect=GrokConnectionUnknown("no token response"),
    ) as exchange:
        with pytest.raises(GrokConnectionUnknown):
            gateway.finish_grok(project_id, account_id)
        assert decode_state(gateway.secrets.read(account_id))["kind"] == "unknown"
        with pytest.raises(GrokConnectionUnknown):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()


def test_finish_records_denial_and_never_replays_device_grant(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    with patch(
        "onefoundry.model_gateway.finish_grok_device_login",
        side_effect=GrokConnectionDenied("denied"),
    ) as exchange:
        with pytest.raises(GrokConnectionDenied):
            gateway.finish_grok(project_id, account_id)
        assert decode_state(gateway.secrets.read(account_id))["reason"] == "denied"
        with pytest.raises(GrokConnectionDenied):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()


def test_oversized_token_response_is_terminal_and_redacted(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    with patch(
        "onefoundry.model_gateway.finish_grok_device_login",
        side_effect=GrokCredentialTooLarge("private-token-content"),
    ) as exchange:
        with pytest.raises(GrokCredentialTooLarge):
            gateway.finish_grok(project_id, account_id)
        assert decode_state(gateway.secrets.read(account_id)) == {
            "kind": "failed",
            "reason": "credential_too_large",
        }
        with pytest.raises(GrokCredentialTooLarge):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()
    endpoint = next(
        route.endpoint
        for route in make_model_router(gateway).routes
        if isinstance(route, APIRoute) and route.path.endswith("/grok/finish")
    )
    with pytest.raises(HTTPException) as response:
        endpoint(project_id, account_id)
    assert response.value.status_code == 409
    assert response.value.detail == "Grok credential exceeds protected storage capacity; connection not saved"
    assert "private-token-content" not in str(response.value)


def test_revision_tag_over_storage_limit_is_terminal(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    connected: dict[str, Any] | None = None
    for length in range(5900, 6200):
        candidate = {
            "kind": "connected",
            "access_token": "A" * length,
            "refresh_token": "B",
            "expires_at": int(time.time()) + 3600,
        }
        try:
            encode_state(candidate)
            encode_state({**candidate, "credential_revision": 1})
        except GrokCredentialTooLarge:
            try:
                encode_state(candidate)
            except GrokCredentialTooLarge:
                continue
            connected = candidate
            break
    assert connected is not None
    with patch("onefoundry.model_gateway.finish_grok_device_login", return_value=connected) as exchange:
        with pytest.raises(GrokCredentialTooLarge):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()
    assert not engine.ready
    assert decode_state(gateway.secrets.read(account_id))["reason"] == "credential_too_large"


def test_expired_or_invalid_pending_state_never_calls_provider(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    with patch("onefoundry.model_gateway.finish_grok_device_login") as exchange:
        state = decode_state(gateway.secrets.read(account_id))
        state["expires_at"] = int(time.time()) - 1
        state["next_poll_at"] = time.time() + 300
        gateway.secrets.write(account_id, encode_state(state))
        with pytest.raises(GrokConnectionExpired):
            gateway.finish_grok(project_id, account_id)
        assert decode_state(gateway.secrets.read(account_id))["reason"] == "expired"

        state["expires_at"] = int(time.time()) + 300
        state["next_poll_at"] = "bad"
        gateway.secrets.write(account_id, encode_state(state))
        with pytest.raises(GrokConnectionError, match="saved device challenge is invalid"):
            gateway.finish_grok(project_id, account_id)
    exchange.assert_not_called()


def test_provider_interval_over_challenge_lifetime_remains_valid(tmp_path: Path) -> None:
    gateway, _, project_id, account_id = _gateway(tmp_path)
    state = decode_state(gateway.secrets.read(account_id))
    state["interval_seconds"] = 1200
    state["next_poll_at"] = time.time() + 1200
    gateway.secrets.write(account_id, encode_state(state))
    shown = gateway.grok_challenge(project_id, account_id)
    assert shown is not None and shown["interval_seconds"] == 1200
    with patch("onefoundry.model_gateway.finish_grok_device_login") as exchange:
        with pytest.raises(GrokConnectionPending) as pending:
            gateway.finish_grok(project_id, account_id)
    assert pending.value.interval_seconds == 1200
    exchange.assert_not_called()


def test_restart_reuses_pending_account_and_cannot_overwrite_connected_state(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    gateway.secrets.write(account_id, encode_state({"kind": "unknown"}))
    fresh = DeviceChallenge("https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device")
    with patch("onefoundry.model_gateway.start_grok_device_login", return_value=fresh) as start:
        restarted = gateway.restart_grok(project_id, account_id)
    assert restarted is not None and restarted["user_code"] == "NEW-CODE"
    assert decode_state(gateway.secrets.read(account_id))["device_code"] == "new-device"
    start.assert_called_once()
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        ),
    )
    with patch("onefoundry.model_gateway.start_grok_device_login") as no_start:
        with pytest.raises(ValueError):
            gateway.restart_grok(project_id, account_id)
    no_start.assert_not_called()


def test_reconnect_starts_new_challenge_on_same_connected_account(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    prior = {
        "kind": "connected",
        "access_token": "private-access",
        "refresh_token": "private-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    gateway.secrets.write(account_id, encode_state(prior))
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    with patch("onefoundry.model_gateway.start_grok_device_login", side_effect=GrokConnectionError("down")):
        with pytest.raises(GrokConnectionError):
            gateway.reconnect_grok(project_id, account_id)
    assert engine.ready and decode_state(gateway.secrets.read(account_id)) == prior
    assert engine.revision == 0 and engine.catalog_valid

    with patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge):
        restarted = gateway.reconnect_grok(project_id, account_id)
    assert restarted is not None and restarted["user_code"] == "NEW-CODE"
    assert not engine.ready and not engine.catalog_valid and engine.revision == 1
    assert decode_state(gateway.secrets.read(account_id))["device_code"] == "new-device"
    assert len([sql for sql in engine.statements if "FOR UPDATE" in sql]) == 1


def test_reconnect_secret_write_failure_keeps_old_token_recoverable(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    prior = {
        "kind": "connected",
        "access_token": "private-access",
        "refresh_token": "private-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    gateway.secrets.write(account_id, encode_state(prior))
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    with (
        patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge),
        patch.object(gateway.secrets, "write", side_effect=OSError("disk unavailable")),
        pytest.raises(OSError),
    ):
        gateway.reconnect_grok(project_id, account_id)
    assert engine.ready
    assert decode_state(gateway.secrets.read(account_id)) == prior
    assert engine.catalog_valid and engine.revision == 0
    shown = gateway._account(project_id, account_id)
    assert shown is not None and shown["credential_present"]


def test_restart_recovers_interrupted_refresh_state(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(account_id, encode_state({"kind": "refreshing"}))
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    with patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge):
        renewed = gateway.restart_grok(project_id, account_id)
    assert renewed is not None and renewed["user_code"] == "NEW-CODE"
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "pending"
    assert not engine.ready and engine.revision == 1


def test_ready_account_with_missing_secret_can_restart(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.remove(account_id)
    shown = gateway._account(project_id, account_id)
    assert shown is not None and not shown["credential_present"]
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    with patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge):
        renewed = gateway.restart_grok(project_id, account_id)
    assert renewed is not None and renewed["user_code"] == "NEW-CODE"
    assert not engine.ready and not engine.catalog_valid and engine.revision == 1


def test_ready_account_with_malformed_connected_secret_can_restart(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "private token with spaces",
                "refresh_token": "private-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        ),
    )
    account = gateway._account(project_id, account_id)
    assert account is not None and not account["credential_present"]
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    with patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge):
        gateway.restart_grok(project_id, account_id)
    assert not engine.ready
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "pending"


def test_reconnect_rollback_after_pending_write_stays_disconnected_and_can_finish(
    tmp_path: Path,
) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        ),
    )
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    original_execute = _Connection.execute

    def fail_update(self: _Connection, statement: object, params: object) -> _Row:
        if str(statement).startswith("UPDATE model_accounts SET credential_ready=false"):
            raise RuntimeError("database write failed")
        return original_execute(self, statement, params)

    with (
        patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge),
        patch.object(_Connection, "execute", fail_update),
        pytest.raises(RuntimeError, match="database write failed"),
    ):
        gateway.reconnect_grok(project_id, account_id)
    assert engine.ready and engine.catalog_valid and engine.revision == 0
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "pending"
    shown = gateway._account(project_id, account_id)
    assert shown is not None and not shown["credential_present"] and not shown["catalog_valid"]

    state = decode_state(gateway.secrets.read(account_id))
    state["next_poll_at"] = 0
    gateway.secrets.write(account_id, encode_state(state))
    connected = {
        "kind": "connected",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    with patch("onefoundry.model_gateway.finish_grok_device_login", return_value=connected) as exchange:
        finished = gateway.finish_grok(project_id, account_id)
    exchange.assert_called_once()
    assert finished is not None and finished["credential_present"]
    assert engine.ready and not engine.catalog_valid and engine.revision == 1


def test_saved_new_token_after_finish_rollback_invalidates_old_catalog(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    engine.catalog_valid = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_at": int(time.time()) + 3600,
                "credential_revision": 1,
            }
        ),
    )
    before = gateway._account(project_id, account_id)
    assert before is not None and not before["credential_present"] and not before["catalog_valid"]
    with patch("onefoundry.model_gateway.finish_grok_device_login") as exchange:
        result = gateway.finish_grok(project_id, account_id)
    exchange.assert_not_called()
    assert result is not None and result["credential_present"]
    assert engine.ready and not engine.catalog_valid and engine.revision == 1
    engine.catalog_valid = True
    gateway.finish_grok(project_id, account_id)
    assert engine.catalog_valid and engine.revision == 1


def test_reconnect_serializes_finish_behind_same_row_lock(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        ),
    )
    challenge = DeviceChallenge(
        "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
    )
    writing = threading.Event()
    finish_started = threading.Event()
    release = threading.Event()
    outcomes: list[object] = []
    original_write = gateway.secrets.write

    def blocked_write(account: UUID, value: str) -> None:
        original_write(account, value)
        if decode_state(value)["kind"] == "pending":
            writing.set()
            assert release.wait(timeout=5)

    def do_reconnect() -> None:
        try:
            outcomes.append(gateway.reconnect_grok(project_id, account_id))
        except Exception as error:
            outcomes.append(error)

    def do_finish() -> None:
        finish_started.set()
        try:
            outcomes.append(gateway.finish_grok(project_id, account_id))
        except Exception as error:
            outcomes.append(error)

    with (
        patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge),
        patch.object(gateway.secrets, "write", side_effect=blocked_write),
    ):
        thread = threading.Thread(target=do_reconnect)
        thread.start()
        assert writing.wait(timeout=5)
        assert not any("credential_ready=false" in sql for sql in engine.statements)
        # The pending challenge is already saved, but finish cannot enter its
        # own row transaction until reconnect finishes updating metadata.
        finish_thread = threading.Thread(target=do_finish)
        finish_thread.start()
        assert finish_started.wait(timeout=5)
        assert not outcomes
        release.set()
        thread.join(timeout=5)
        finish_thread.join(timeout=5)
    assert not thread.is_alive() and not finish_thread.is_alive()
    assert len(outcomes) == 2
    assert sum(isinstance(result, GrokConnectionPending) for result in outcomes) == 1
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "pending"
    assert not engine.ready and not engine.catalog_valid


def test_saved_connected_state_must_have_usable_credential_shape(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    gateway.secrets.write(account_id, encode_state({"kind": "connected", "access_token": "private-access"}))
    with pytest.raises(GrokConnectionError, match="credential is invalid"):
        gateway.finish_grok(project_id, account_id)
    assert not engine.ready
    engine.ready = True
    with pytest.raises(GrokConnectionError, match="credential is invalid"):
        gateway.grok_credential(project_id, account_id)


def test_refresh_unknown_never_replays_rotating_token(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_at": int(time.time()) - 1,
            }
        ),
    )

    def refresh(*args: object) -> dict[str, Any]:
        assert decode_state(gateway.secrets.read(account_id))["kind"] == "refreshing"
        raise GrokConnectionUnknown("outcome unknown")

    with patch("onefoundry.model_gateway.refresh_grok_credential", side_effect=refresh) as call:
        with pytest.raises(GrokConnectionUnknown):
            gateway.grok_credential(project_id, account_id)
        with pytest.raises(GrokConnectionError, match="not connected"):
            gateway.grok_credential(project_id, account_id)
    call.assert_called_once()
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "unknown"
    assert not engine.ready and not engine.catalog_valid and engine.revision == 1


def test_refresh_success_saves_rotated_token_and_keeps_account_ready(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
            }
        ),
    )
    rotated = {
        "kind": "connected",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    with patch("onefoundry.model_gateway.refresh_grok_credential", return_value=rotated) as refresh:
        result = gateway.grok_credential(project_id, account_id)
    refresh.assert_called_once()
    assert result == {**rotated, "credential_revision": 1}
    assert decode_state(gateway.secrets.read(account_id)) == result
    assert engine.ready and engine.revision == 1


def test_refresh_token_survives_metadata_rollback_without_exposing_old_catalog(
    tmp_path: Path,
) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
            }
        ),
    )
    rotated = {
        "kind": "connected",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": int(time.time()) + 3600,
    }
    original_execute = _Connection.execute

    def fail_revision(self: _Connection, statement: object, params: object) -> _Row:
        if str(statement).startswith("UPDATE model_accounts SET credential_revision="):
            raise RuntimeError("database write failed")
        return original_execute(self, statement, params)

    with (
        patch("onefoundry.model_gateway.refresh_grok_credential", return_value=rotated) as refresh,
        patch.object(_Connection, "execute", fail_revision),
        pytest.raises(RuntimeError, match="database write failed"),
    ):
        gateway.grok_credential(project_id, account_id)
    refresh.assert_called_once()
    assert engine.ready and engine.catalog_valid and engine.revision == 0
    assert decode_state(gateway.secrets.read(account_id)) == {**rotated, "credential_revision": 1}
    shown = gateway._account(project_id, account_id)
    assert shown is not None and not shown["credential_present"] and not shown["catalog_valid"]
    with patch("onefoundry.model_gateway.refresh_grok_credential") as no_refresh:
        gateway.finish_grok(project_id, account_id)
    no_refresh.assert_not_called()
    assert engine.ready and not engine.catalog_valid and engine.revision == 1


def test_refresh_oversized_credential_requires_reconnect(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
            }
        ),
    )
    with patch(
        "onefoundry.model_gateway.refresh_grok_credential",
        side_effect=GrokCredentialTooLarge("private-token-content"),
    ) as refresh:
        with pytest.raises(GrokCredentialTooLarge):
            gateway.grok_credential(project_id, account_id)
    refresh.assert_called_once()
    assert decode_state(gateway.secrets.read(account_id)) == {
        "kind": "failed",
        "reason": "credential_too_large",
    }
    assert not engine.ready and not engine.catalog_valid and engine.revision == 1


def test_refresh_definitive_rejection_requires_reconnect(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(
        account_id,
        encode_state(
            {
                "kind": "connected",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
            }
        ),
    )
    with patch(
        "onefoundry.model_gateway.refresh_grok_credential", side_effect=GrokConnectionError("expired")
    ) as refresh:
        with pytest.raises(GrokConnectionError):
            gateway.grok_credential(project_id, account_id)
    refresh.assert_called_once()
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "failed"
    assert not engine.ready and not engine.catalog_valid and engine.revision == 1


def test_interrupted_refresh_state_disables_account_without_provider_call(tmp_path: Path) -> None:
    gateway, engine, project_id, account_id = _gateway(tmp_path)
    engine.ready = True
    gateway.secrets.write(account_id, encode_state({"kind": "refreshing"}))
    with patch("onefoundry.model_gateway.refresh_grok_credential") as call:
        with pytest.raises(GrokConnectionUnknown):
            gateway.grok_credential(project_id, account_id)
    call.assert_not_called()
    assert decode_state(gateway.secrets.read(account_id))["kind"] == "unknown"
    assert not engine.ready and engine.revision == 1


@pytest.mark.parametrize(
    ("error", "status", "message", "retry_after"),
    [
        (GrokConnectionPending(interval_seconds=10), 409, "Complete Grok sign-in, then check again", "10"),
        (
            GrokConnectionExpired("expired"),
            409,
            "Grok sign-in expired; start fresh sign-in on this account",
            None,
        ),
        (
            GrokConnectionDenied("denied"),
            409,
            "Grok sign-in was denied; start fresh sign-in on this account",
            None,
        ),
        (
            GrokConnectionUnknown("private-device"),
            502,
            "Grok token exchange outcome is unknown; start fresh sign-in on this account",
            None,
        ),
    ],
)
def test_finish_api_reports_redacted_distinct_status_and_retry_interval(
    error: Exception,
    status: int,
    message: str,
    retry_after: str | None,
) -> None:
    def finish(project_id: UUID, account_id: UUID) -> None:
        raise error

    fake = SimpleNamespace(finish_grok=finish)
    router = make_model_router(cast(ModelGateway, fake))
    endpoint = next(
        route.endpoint
        for route in router.routes
        if isinstance(route, APIRoute) and route.path.endswith("/grok/finish")
    )
    with pytest.raises(HTTPException) as raised:
        endpoint(uuid4(), uuid4())
    assert raised.value.status_code == status
    assert raised.value.detail == message
    assert (raised.value.headers or {}).get("Retry-After") == retry_after
    assert "private-device" not in str(raised.value)
