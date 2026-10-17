"""Codex account login, refresh, catalog, and secret redaction without egress."""

import base64
import json
import time
from typing import cast
from urllib.request import Request

import pytest

from onefoundry.codex_account import (
    CodexConnectionError,
    CodexConnectionPending,
    CodexDeviceStartError,
    CodexTokenExchangeUnknown,
    decode_state,
    discover_codex_models,
    encode_state,
    finish_device_login,
    refresh_credential,
    start_device_login,
)
from onefoundry.inference import TransportError
from onefoundry.inference.http import HTTPResult
from onefoundry.model_providers import DiscoveryError
from onefoundry.settings import Settings


def _jwt() -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "account-private"}}
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "header." + segment + ".signature"


def test_device_login_and_model_discovery_keep_credentials_off_browser_contract() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        assert timeout == (60 if outbound.full_url.endswith("/oauth/token") else 5)
        assert outbound.get_header("User-agent") == "OneFoundry/0.1"
        assert outbound.get_header("Originator") is None
        if outbound.full_url.endswith("/deviceauth/usercode"):
            return HTTPResult(
                200, b'{"device_auth_id":"private-device","user_code":"ABCD-EFGH","interval":5}'
            )
        if outbound.full_url.endswith("/deviceauth/token"):
            body = json.loads(cast(bytes, outbound.data))
            assert body == {"device_auth_id": "private-device", "user_code": "ABCD-EFGH"}
            return HTTPResult(
                200, b'{"authorization_code":"private-code","code_verifier":"private-verifier"}'
            )
        if outbound.full_url.endswith("/oauth/token"):
            assert b"private-code" in cast(bytes, outbound.data)
            return HTTPResult(
                200,
                json.dumps(
                    {"access_token": _jwt(), "refresh_token": "private-refresh", "expires_in": 3600}
                ).encode(),
            )
        assert outbound.full_url.startswith("https://chatgpt.com/backend-api/codex/models?client_version=")
        assert outbound.get_header("Authorization") == "Bearer " + _jwt()
        assert outbound.get_header("Chatgpt-account-id") == "account-private"
        return HTTPResult(
            200,
            b'{"models":[{"slug":"gpt-test","display_name":"GPT Test",'
            b'"supported_reasoning_levels":[{"effort":"low"},{"effort":"high"}]},'
            b'{"slug":"hidden","visibility":"hidden"}]}',
        )

    settings = Settings(db_password="test")
    challenge = start_device_login(settings, http)
    assert challenge.verification_url == "https://auth.openai.com/codex/device"
    assert challenge.user_code == "ABCD-EFGH" and challenge.device_code == "private-device"
    pending = {
        "kind": "pending",
        "device_code": challenge.device_code,
        "user_code": challenge.user_code,
        "expires_at": challenge.expires_at,
    }
    state = finish_device_login(pending, settings, http)
    assert state["account_id"] == "account-private"
    assert state["refresh_token"] == "private-refresh"
    assert decode_state(encode_state(state)) == state
    models = discover_codex_models(state, settings, http)
    assert [(item.model_id, item.category) for item in models] == [("gpt-test", "text")]
    assert models[0].reasoning_efforts == ("low", "high")
    assert len(calls) == 4


def test_pending_is_observable_without_token_exchange() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return HTTPResult(428, b"")

    with pytest.raises(CodexConnectionPending):
        finish_device_login(
            {
                "kind": "pending",
                "device_code": "secret",
                "user_code": "ABCD",
                "expires_at": int(time.time()) + 100,
            },
            Settings(db_password="test"),
            http,
        )
    assert len(calls) == 1


def test_successful_one_time_exchange_is_saved_before_credential_validation() -> None:
    saved: list[dict[str, object]] = []
    calls: list[str] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound.full_url)
        if outbound.full_url.endswith("/deviceauth/token"):
            return HTTPResult(
                200, b'{"authorization_code":"private-code","code_verifier":"private-verifier"}'
            )
        return HTTPResult(200, b'{"access_token":"private-access","refresh_token":"private-refresh"}')

    pending = {
        "kind": "pending",
        "device_code": "private-device",
        "user_code": "ABCD-EFGH",
        "expires_at": int(time.time()) + 100,
    }
    settings = Settings(db_password="test")
    with pytest.raises(CodexConnectionError, match="did not identify the account"):
        finish_device_login(pending, settings, http, on_exchange=saved.append)
    assert saved == [
        {
            "kind": "exchanged",
            "tokens": {"access_token": "private-access", "refresh_token": "private-refresh"},
        }
    ]
    assert len(calls) == 2
    with pytest.raises(CodexConnectionError, match="did not identify the account"):
        finish_device_login(saved[0], settings, http)
    assert len(calls) == 2


def test_saved_codex_exchange_accepts_provider_ten_day_access_expiry() -> None:
    now = int(time.time())
    saved = {
        "kind": "exchanged",
        "tokens": {
            "access_token": _jwt(),
            "refresh_token": "private-refresh",
            "expires_in": 864000,
        },
    }

    def unexpected_http(outbound: Request, *, timeout: float) -> HTTPResult:
        raise AssertionError("saved exchange must not request another token")

    connected = finish_device_login(saved, Settings(db_password="test"), unexpected_http)
    assert connected["kind"] == "connected"
    assert connected["account_id"] == "account-private"
    assert now + 864000 <= connected["expires_at"] <= int(time.time()) + 864000


def test_device_login_accepts_token_response_without_expires_in() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("/deviceauth/token"):
            return HTTPResult(
                200, b'{"authorization_code":"private-code","code_verifier":"private-verifier"}'
            )
        assert outbound.full_url.endswith("/oauth/token")
        return HTTPResult(
            200,
            json.dumps(
                {"access_token": "private-access", "id_token": _jwt(), "refresh_token": "private-refresh"}
            ).encode(),
        )

    state = finish_device_login(
        {
            "kind": "pending",
            "device_code": "private-device",
            "user_code": "ABCD-EFGH",
            "expires_at": int(time.time()) + 100,
        },
        Settings(db_password="test"),
        http,
    )
    assert state["kind"] == "connected"
    assert state["account_id"] == "account-private"
    assert state["expires_at"] is None


def test_one_time_codex_exchange_uses_auth_timeout_not_discovery_timeout() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("/deviceauth/token"):
            assert timeout == 5
            return HTTPResult(
                200, b'{"authorization_code":"private-code","code_verifier":"private-verifier"}'
            )
        assert outbound.full_url.endswith("/oauth/token")
        assert timeout == 60
        return HTTPResult(
            200,
            json.dumps({"access_token": _jwt(), "refresh_token": "private-refresh"}).encode(),
        )

    state = finish_device_login(
        {
            "kind": "pending",
            "device_code": "private-device",
            "user_code": "ABCD-EFGH",
            "expires_at": int(time.time()) + 100,
        },
        Settings(db_password="test"),
        http,
    )
    assert state["kind"] == "connected"


def test_one_time_exchange_transport_failure_has_unknown_outcome() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        if outbound.full_url.endswith("/deviceauth/token"):
            return HTTPResult(
                200, b'{"authorization_code":"private-code","code_verifier":"private-verifier"}'
            )
        assert outbound.full_url.endswith("/oauth/token")
        assert timeout == 60
        raise TransportError("private upstream detail")

    with pytest.raises(CodexTokenExchangeUnknown) as caught:
        finish_device_login(
            {
                "kind": "pending",
                "device_code": "private-device",
                "user_code": "ABCD-EFGH",
                "expires_at": int(time.time()) + 100,
            },
            Settings(db_password="test"),
            http,
        )
    assert "private upstream detail" not in str(caught.value)


def test_device_start_reports_status_without_parsing_provider_error_body() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        assert timeout == 5
        assert outbound.full_url.endswith("/deviceauth/usercode")
        return HTTPResult(530, b"upstream-private-error-body")

    with pytest.raises(CodexDeviceStartError) as caught:
        start_device_login(Settings(db_password="test"), http)
    assert caught.value.status == 530
    assert "upstream-private-error-body" not in str(caught.value)


def test_refresh_and_invalid_catalog_fail_without_provider_body() -> None:
    settings = Settings(db_password="test")
    old = {
        "kind": "connected",
        "access_token": _jwt(),
        "refresh_token": "old-refresh",
        "account_id": "account-private",
        "expires_at": int(time.time()) - 1,
    }

    def refresh_http(outbound: Request, *, timeout: float) -> HTTPResult:
        assert outbound.full_url.endswith("/oauth/token")
        assert timeout == 60
        return HTTPResult(200, json.dumps({"access_token": _jwt(), "expires_in": 3600}).encode())

    new = refresh_credential(old, settings, refresh_http)
    assert new["refresh_token"] == "old-refresh"

    def invalid_http(outbound: Request, *, timeout: float) -> HTTPResult:
        return HTTPResult(200, b'{"models":[{"slug":"one"},{"slug":"one"}]}')

    with pytest.raises(DiscoveryError):
        discover_codex_models(new, settings, invalid_http)

    def invalid_reasoning_http(outbound: Request, *, timeout: float) -> HTTPResult:
        return HTTPResult(
            200,
            b'{"models":[{"slug":"one","supported_reasoning_levels":[{"effort":"bad level"}]}]}',
        )

    with pytest.raises(DiscoveryError, match="invalid reasoning levels"):
        discover_codex_models(new, settings, invalid_reasoning_http)


@pytest.mark.parametrize("version", [None, "0.157.0"])
def test_discovery_catalog_version_controls_provider_reported_models(version: str | None) -> None:
    settings = (
        Settings(db_password="test")
        if version is None
        else Settings(db_password="test", codex_catalog_client_version=version)
    )
    calls: list[str] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound.full_url)
        expected = version or "0.160.0"
        assert outbound.full_url.endswith("client_version=" + expected)
        assert outbound.method == "GET" and outbound.data is None
        assert timeout == settings.model_discovery_timeout_seconds
        models: list[dict[str, object]] = [{"slug": "gpt-6-sol"}]
        if expected == "0.160.0":
            models.insert(
                0,
                {
                    "slug": "gpt-6.1-sol",
                    "display_name": "GPT-6.1 Sol",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                },
            )
        return HTTPResult(200, json.dumps({"models": models}).encode())

    models = discover_codex_models(
        {"kind": "connected", "access_token": "private-access", "account_id": "private-account"},
        settings,
        http,
    )
    assert len(calls) == 1
    assert [m.model_id for m in models] == (
        ["gpt-6.1-sol", "gpt-6-sol"] if version is None else ["gpt-6-sol"]
    )
    if version is None:
        assert models[0].reasoning_efforts == ("ultra",)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_discovery_http_failure_reports_status_without_body(status: int) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        return HTTPResult(status, b"private-provider-body")

    with pytest.raises(DiscoveryError, match=f"HTTP {status}") as caught:
        discover_codex_models(
            {"kind": "connected", "access_token": "private-access", "account_id": "private-account"},
            Settings(db_password="test"),
            http,
        )
    assert "private" not in str(caught.value)


def test_discovery_transport_failure_is_discovery_error_without_reconnection_claim() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        raise TransportError("private-provider-error")

    with pytest.raises(DiscoveryError, match="transport failed") as caught:
        discover_codex_models(
            {"kind": "connected", "access_token": "private-access", "account_id": "private-account"},
            Settings(db_password="test"),
            http,
        )
    assert "private" not in str(caught.value) and "reconnect" not in str(caught.value)
