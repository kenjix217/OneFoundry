"""Grok subscription account flow against an injected, no-egress transport."""

import base64
import json
import time
from typing import cast
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

from onefoundry.grok_account import (
    GrokConnectionError,
    GrokConnectionPending,
    GrokConnectionUnknown,
    GrokCredentialTooLarge,
    decode_state,
    discover_grok_models,
    encode_state,
    finish_device_login,
    refresh_credential,
    start_device_login,
)
from onefoundry.inference.http import HTTPResult
from onefoundry.model_providers import DiscoveryError
from onefoundry.settings import Settings

DISCOVERY = {
    "issuer": "https://auth.x.ai",
    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
    "token_endpoint": "https://auth.x.ai/oauth2/token",
}
SETTINGS = Settings(db_password="test", xai_oauth_client_id="test-public-client")


def _response(data: object, status: int = 200) -> HTTPResult:
    return HTTPResult(status, json.dumps(data).encode())


def _claims() -> str:
    subject = base64.urlsafe_b64encode(json.dumps({"sub": "private-subject"}).encode()).decode().rstrip("=")
    return "header." + subject + ".signature"


def test_device_login_finish_and_catalog() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        assert timeout == 5
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        if outbound.full_url.endswith("/device/code"):
            form = parse_qs(cast(bytes, outbound.data).decode())
            assert form["client_id"] == ["test-public-client"]
            assert "offline_access" in form["scope"][0]
            return _response(
                {
                    "device_code": "private-device",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.x.ai/activate",
                    "expires_in": 300,
                    "interval": 5,
                }
            )
        if outbound.full_url.endswith("/token"):
            form = parse_qs(cast(bytes, outbound.data).decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
            assert form["device_code"] == ["private-device"]
            return _response(
                {
                    "access_token": "private-access",
                    "refresh_token": "private-refresh",
                    "id_token": _claims(),
                    "expires_in": 3600,
                }
            )
        assert outbound.full_url == "https://cli-chat-proxy.grok.com/v1/models"
        assert outbound.get_header("Authorization") == "Bearer private-access"
        assert outbound.get_header("X-xai-token-auth") == "xai-grok-cli"
        return _response(
            {
                "data": [
                    {"id": "grok-4", "name": "Grok 4", "api_backend": "responses"},
                    {"model": "grok-4-fast", "backend": "chat"},
                    {"id": "grok-chat", "apiBackend": "chat_completions"},
                    {"id": "image-only", "backend": "image"},
                    {"id": "grok-4"},
                    {"id": "vendor/grok-4", "backend": "responses"},
                    {"id": "bad\nmodel"},
                ]
            }
        )

    challenge = start_device_login(SETTINGS, http)
    assert challenge.verification_url == "https://auth.x.ai/activate"
    assert challenge.user_code == "ABCD-EFGH" and challenge.device_code == "private-device"
    assert challenge.interval_seconds == 5
    assert "private-device" not in repr(challenge)
    state = finish_device_login(
        {"kind": "pending", "device_code": challenge.device_code, "expires_at": challenge.expires_at},
        SETTINGS,
        http,
    )
    assert state["access_token"] == "private-access"
    assert state["refresh_token"] == "private-refresh"
    assert len(state["account_id_hash"]) == 64
    assert "private-subject" not in str(state)
    assert decode_state(encode_state(state)) == state
    models = discover_grok_models(state, SETTINGS, http)
    assert [(model.model_id, model.category) for model in models] == [
        ("grok-4", "text"),
        ("grok-4-fast", "text"),
        ("grok-chat", "text"),
    ]
    assert len(calls) == 5


@pytest.mark.parametrize("code", ["authorization_pending", "slow_down"])
def test_device_poll_pending_is_distinct_from_failure(code: str) -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return _response(DISCOVERY) if len(calls) == 1 else _response({"error": code}, 400)

    with pytest.raises(GrokConnectionPending, match="check again"):
        finish_device_login(
            {"kind": "pending", "device_code": "private-device", "expires_at": int(time.time()) + 100},
            SETTINGS,
            http,
        )
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [("expired_token", "expired"), ("access_denied", "denied"), ("unknown", "failed")],
)
def test_device_poll_errors_are_redacted(error: str, message: str) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response({"error": error, "error_description": "private-refresh"}, 400)

    with pytest.raises(GrokConnectionError, match=message) as raised:
        finish_device_login(
            {"kind": "pending", "device_code": "private-device", "expires_at": int(time.time()) + 100},
            SETTINGS,
            http,
        )
    assert "private-refresh" not in str(raised.value)
    assert "private-device" not in str(raised.value)


def test_refresh_uses_rotated_token_and_retains_old_when_not_rotated() -> None:
    old = {
        "kind": "connected",
        "access_token": "old-access",
        "refresh_token": "private-refresh",
        "expires_at": 1,
        "account_id_hash": "hash",
    }

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        form = parse_qs(cast(bytes, outbound.data).decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["private-refresh"]
        return _response({"access_token": "new-access", "expires_in": 3600})

    new = refresh_credential(old, SETTINGS, http)
    assert new["access_token"] == "new-access"
    assert new["refresh_token"] == "private-refresh"
    assert new["account_id_hash"] == "hash"


def test_untrusted_discovery_and_missing_client_make_no_token_request() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return _response({**DISCOVERY, "token_endpoint": "https://auth.x.ai.attacker.example/token"})

    with pytest.raises(GrokConnectionError, match="not configured"):
        start_device_login(Settings(db_password="test", xai_oauth_client_id=""), http)
    assert calls == []
    with pytest.raises(GrokConnectionError, match="untrusted"):
        start_device_login(SETTINGS, http)
    assert len(calls) == 1


def test_device_verification_url_rejects_query_values() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response(
            {
                "device_code": "private-device",
                "user_code": "ABCD",
                "verification_uri": "https://auth.x.ai/activate?device_code=private-device",
            }
        )

    with pytest.raises(GrokConnectionError, match="untrusted") as raised:
        start_device_login(SETTINGS, http)
    assert "private-device" not in str(raised.value)


def test_invalid_token_and_catalog_fail_without_response_secrets() -> None:
    def incomplete(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response(
            {
                "access_token": "private-access",
                "error_description": "private-secret",
                "expires_in": 3600,
            }
        )

    with pytest.raises(GrokConnectionError) as raised:
        finish_device_login(
            {"kind": "pending", "device_code": "private-device", "expires_at": int(time.time()) + 100},
            SETTINGS,
            incomplete,
        )
    assert "private-secret" not in str(raised.value)
    assert "private-access" not in str(raised.value)

    def empty_catalog(outbound: Request, *, timeout: float) -> HTTPResult:
        return _response({"data": [{"id": "image-only", "backend": "image"}]})

    with pytest.raises(DiscoveryError, match="no usable models"):
        discover_grok_models({"kind": "connected", "access_token": "private-access"}, SETTINGS, empty_catalog)


def test_expired_pending_state_and_refresh_rejection() -> None:
    def no_calls(outbound: Request, *, timeout: float) -> HTTPResult:
        pytest.fail("expired device state must not make a network request")

    with pytest.raises(GrokConnectionError, match="expired"):
        finish_device_login(
            {"kind": "pending", "device_code": "private-device", "expires_at": int(time.time()) - 1},
            SETTINGS,
            no_calls,
        )

    def denied(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response({"error": "invalid_grant", "error_description": "private-refresh"}, 401)

    with pytest.raises(GrokConnectionError, match="reconnection") as raised:
        refresh_credential({"kind": "connected", "refresh_token": "private-refresh"}, SETTINGS, denied)
    assert "private-refresh" not in str(raised.value)


def test_transport_exception_is_redacted() -> None:
    def broken(outbound: Request, *, timeout: float) -> HTTPResult:
        raise OSError("private-device private-refresh")

    with pytest.raises(GrokConnectionError) as raised:
        start_device_login(SETTINGS, broken)
    assert "private-device" not in str(raised.value)
    assert "private-refresh" not in str(raised.value)


def test_slow_down_increases_the_persisted_poll_interval() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response({"error": "slow_down"}, 400)

    with pytest.raises(GrokConnectionPending) as raised:
        finish_device_login(
            {
                "kind": "pending",
                "device_code": "test-device",
                "expires_at": int(time.time()) + 100,
                "interval_seconds": 40,
            },
            SETTINGS,
            http,
        )
    assert getattr(raised.value, "interval_seconds", None) == 45


def test_discovery_rejects_a_different_issuer_before_credentials_leave() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return _response({**DISCOVERY, "issuer": "https://other.x.ai"})

    with pytest.raises(GrokConnectionError, match="issuer"):
        start_device_login(SETTINGS, http)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["transport", "malformed", "server", "bad-token"])
def test_token_exchange_unknown_outcomes_do_not_expose_provider_content(failure: str) -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        if failure == "transport":
            raise TimeoutError("private-device")
        if failure == "malformed":
            return HTTPResult(200, b"private-access")
        if failure == "server":
            return _response({"error": "private-access"}, 503)
        return _response({"access_token": "bad\r\ntoken", "refresh_token": "test-refresh", "expires_in": 300})

    with pytest.raises(GrokConnectionUnknown) as raised:
        finish_device_login(
            {"kind": "pending", "device_code": "private-device", "expires_at": int(time.time()) + 100},
            SETTINGS,
            http,
        )
    assert len(calls) == 2
    assert "private-" not in str(raised.value)


def test_device_flow_preserves_provider_interval_longer_than_thirty_seconds() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response(
            {
                "device_code": "test-device",
                "user_code": "TEST-CODE",
                "verification_uri": "https://accounts.x.ai/device",
                "interval": 60,
                "expires_in": 600,
            }
        )

    assert start_device_login(SETTINGS, http).interval_seconds == 60


@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "server", "invalid-token"])
def test_refresh_ambiguity_is_distinct_from_rejection(failure: str) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        if failure == "timeout":
            raise TimeoutError("test-refresh must stay private")
        if failure == "invalid-json":
            return HTTPResult(200, b"test-access")
        if failure == "server":
            return _response({"error": "test-refresh"}, 503)
        return _response({"access_token": "invalid\nheader", "expires_in": 3600})

    with pytest.raises(GrokConnectionUnknown) as raised:
        refresh_credential({"kind": "connected", "refresh_token": "test-refresh"}, SETTINGS, http)
    assert "test-refresh" not in str(raised.value)
    assert "test-access" not in str(raised.value)


@pytest.mark.parametrize("interval", [True, -1, "5.5", "9" * 5000])
def test_invalid_provider_poll_interval_is_a_redacted_boundary_failure(interval: object) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response(
            {
                "device_code": "test-device",
                "user_code": "TEST-CODE",
                "verification_uri": "https://accounts.x.ai/device",
                "interval": interval,
            }
        )

    with pytest.raises(GrokConnectionError, match="polling interval"):
        start_device_login(SETTINGS, http)


def test_oversized_renewable_credentials_fail_before_secret_store_handoff() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("openid-configuration"):
            return _response(DISCOVERY)
        return _response({"access_token": "a" * 3100, "refresh_token": "b" * 3100, "expires_in": 3600})

    pending = {"kind": "pending", "device_code": "test-device", "expires_at": int(time.time()) + 100}
    with pytest.raises(GrokCredentialTooLarge, match="storage capacity") as raised:
        finish_device_login(pending, SETTINGS, http)
    assert "aaa" not in str(raised.value) and "bbb" not in str(raised.value)
    with pytest.raises(GrokCredentialTooLarge):
        refresh_credential({"kind": "connected", "refresh_token": "old-test"}, SETTINGS, http)
