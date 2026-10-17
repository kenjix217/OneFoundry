"""Broker destination and request-envelope admission before any external send."""

from __future__ import annotations

import socket
import ssl
import sys
from io import BytesIO
from types import SimpleNamespace
from typing import cast

import pytest

from runner.codex_team_broker_child import (
    BrokerConfig,
    BrokerProtocolError,
    _credential_from_stdin,
    _read_connect,
    _read_request,
)
from runner.codex_team_broker_policy import TeamRequestLimits


def _config() -> BrokerConfig:
    return BrokerConfig(
        "provider.test",
        "/v1/responses",
        "127.0.0.1",
        TeamRequestLimits("gpt-6-sol", 131072, 6),
        65536,
        "/tls/cert.pem",
    )


def test_provider_route_requires_exact_host_and_trust() -> None:
    _config()
    BrokerConfig("api.openai.com", "/v1/responses", "api.openai.com", _config().limits, 65536, None)
    BrokerConfig("chatgpt.com", "/backend-api/codex/responses", "chatgpt.com", _config().limits, 65536, None)
    for host, path, upstream, ca in (
        ("other.test", "/v1/responses", "127.0.0.1", "/tls/cert.pem"),
        ("api.openai.com", "/backend-api/codex/responses", "api.openai.com", None),
        ("chatgpt.com", "/v1/responses", "chatgpt.com", None),
        ("api.openai.com", "/v1/responses", "127.0.0.1", None),
        ("api.openai.com", "/v1/responses", "api.openai.com", "/tls/cert.pem"),
        ("provider.test", "/v1/responses", "example.com", "/tls/cert.pem"),
        ("provider.test", "/v1/responses", "127.0.0.1", None),
    ):
        with pytest.raises(ValueError):
            BrokerConfig(host, path, upstream, _config().limits, 65536, ca)


def test_connect_and_request_envelope_reject_wrong_destination_or_credential() -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(b"CONNECT other.test:443 HTTP/1.1\r\nHost: other.test\r\n\r\n")
        with pytest.raises(BrokerProtocolError, match="connect_target_denied"):
            _read_connect(left, "provider.test")
    finally:
        left.close()
        right.close()
    left, right = socket.socketpair()
    try:
        right.sendall(
            b"POST /v1/responses HTTP/1.1\r\nHost: provider.test\r\n"
            b"Authorization: Bearer real-secret\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        with pytest.raises(BrokerProtocolError, match="client_placeholder_invalid"):
            _read_request(cast(ssl.SSLSocket, left), _config())
    finally:
        left.close()
        right.close()


def test_account_binding_is_read_only_from_private_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"test-token\naccount-123\n")))
    assert _credential_from_stdin("/backend-api/codex/responses") == (b"test-token", b"account-123")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"test-token\nwrong id\n")))
    with pytest.raises(ValueError, match="broker account binding unavailable"):
        _credential_from_stdin("/backend-api/codex/responses")
