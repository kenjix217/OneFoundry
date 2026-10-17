"""Responses SSE framing and safe transport diagnostics at the credential broker."""

import http.client
import json
import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest

from runner import repository_provider_broker as broker
from runner.repository_provider_diagnostics import valid_denial


def forward_body(body: bytes) -> bytes:
    response = MagicMock()
    response.status = 200
    response.getheader.return_value = "text/event-stream"
    response.read.return_value = body
    with (
        patch(
            "runner.repository_provider_broker.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("8.8.8.8", 443))],
        ),
        patch("runner.repository_provider_broker.socket.create_connection"),
        patch("runner.repository_provider_broker.ssl.create_default_context"),
        patch("runner.repository_provider_broker.http.client.HTTPResponse", return_value=response),
    ):
        return broker.forward(
            {
                "upstream_url": "https://provider.test/v1/responses",
                "request_timeout_seconds": 1,
                "max_response_bytes": 4096,
            },
            b"fixture-private-value",
            b"{}",
        )


@pytest.mark.parametrize(
    "body",
    [
        b'data: {"type":"response.completed"}\n\ndata: [DONE]\n\n',
        b': keepalive\r\ndata:{"type":"response.completed"}\r\n\r\n',
        b'data: {\ndata: "type":"response.completed"\ndata: }\n\n',
    ],
)
def test_complete_sse_supports_standard_framing_and_end_marker(body: bytes) -> None:
    assert forward_body(body) == body


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"data: [DONE]\n\n",
        b'data: {"type":"response.failed"}\n\ndata: [DONE]\n\n',
        b'data: {"type":"response.completed"}\n\ndata: {"type":"response.completed"}\n\n',
        b'data: [DONE]\n\ndata: {"type":"response.completed"}\n\n',
        b'data: {"type":"response.completed"}\n\ndata: broken\n\n',
        b'data: {"type":"response.completed","text":"fixture-private-value"}\n\n',
        b"x" * 4097,
    ],
)
def test_end_marker_never_substitutes_for_one_safe_completed_response(body: bytes) -> None:
    with pytest.raises(ValueError, match="^repository_provider_"):
        forward_body(body)


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError("private detail"), "repository_transport_timeout"),
        (socket.gaierror("private detail"), "repository_transport_dns_failed"),
        (ssl.SSLError("private detail"), "repository_transport_tls_failed"),
        (http.client.IncompleteRead(b"private detail"), "repository_transport_incomplete_body"),
        (http.client.RemoteDisconnected("private detail"), "repository_transport_connection_closed"),
        (ConnectionResetError("private detail"), "repository_transport_connection_closed"),
        (BrokenPipeError("private detail"), "repository_transport_connection_closed"),
        (http.client.BadStatusLine("private detail"), "repository_transport_protocol_failed"),
        (json.JSONDecodeError("private detail", "", 0), "repository_provider_response_invalid"),
        (OSError("private detail"), "provider_transport_unavailable"),
        (ValueError("repository_provider_http_503"), "repository_provider_http_503"),
    ],
)
def test_transport_diagnostics_identify_failure_without_exception_content(
    error: Exception, reason: str
) -> None:
    assert broker.failure_reason(error) == reason
    assert valid_denial(reason)
    assert "private" not in reason


@pytest.mark.parametrize(
    "event,reason",
    [
        (
            {"type": "error", "code": "server_is_overloaded"},
            "repository_provider_rejected_server_is_overloaded",
        ),
        (
            {"type": "response.failed", "response": {"error": {"code": "context_length_exceeded"}}},
            "repository_provider_rejected_context_length_exceeded",
        ),
        (
            {"type": "response.failed", "response": {"error": {"code": "private-detail"}}},
            "repository_provider_rejected_unknown",
        ),
        ({"type": "response.incomplete"}, "repository_provider_incomplete"),
        ({"type": "response.created"}, "repository_provider_terminal_missing"),
        ({"type": []}, "repository_provider_response_invalid"),
    ],
)
def test_rejected_stream_retains_only_allowlisted_provider_identity(
    event: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=f"^{reason}$"):
        forward_body(b"data: " + json.dumps({**event, "message": "private detail"}).encode() + b"\n\n")
    assert valid_denial(reason)
