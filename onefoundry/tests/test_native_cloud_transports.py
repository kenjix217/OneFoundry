"""Offline protocol and failure contracts for native cloud text APIs."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.request import Request

import pytest

from onefoundry.inference.contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TransportError,
)
from onefoundry.inference.http import HTTPResult
from onefoundry.inference.native_cloud import AnthropicMessagesTransport, GeminiGenerateContentTransport


def _request(model: str) -> TextRequest:
    return TextRequest(model, "Be brief", "Hello", 100, 12, "request-1")


def _anthropic_payload(**changes: object) -> bytes:
    value: dict[str, object] = {
        "id": "msg_123",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Answer"}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    value.update(changes)
    return json.dumps(value).encode()


def _gemini_payload(**changes: object) -> bytes:
    value: dict[str, object] = {
        "responseId": "resp_123",
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Answer"}]}}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
    }
    value.update(changes)
    return json.dumps(value).encode()


def test_anthropic_native_success() -> None:
    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        assert timeout == 12
        assert outbound.full_url == "https://api.anthropic.com/v1/messages"
        assert outbound.get_header("X-api-key") == "private-key"
        assert outbound.get_header("Anthropic-version") == "2023-06-01"
        assert outbound.get_header("X-client-request-id") == "request-1"
        body: dict[str, Any] = json.loads(cast(bytes, outbound.data))
        assert body == {
            "model": "claude-test",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "stream": False,
            "system": "Be brief",
        }
        assert "tools" not in body
        return HTTPResult(200, _anthropic_payload())

    result = AnthropicMessagesTransport(
        endpoint="https://api.anthropic.com/v1", api_key="private-key", http=fake
    ).generate(_request("anthropic/claude-test"))
    assert (result.text, result.provider_response_id, result.input_tokens, result.output_tokens) == (
        "Answer",
        "msg_123",
        4,
        2,
    )
    assert result.finish_reason == "completed"


def test_gemini_native_success() -> None:
    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        assert timeout == 12
        assert outbound.full_url == (
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
        )
        assert outbound.get_header("X-goog-api-key") == "private-key"
        assert outbound.get_header("X-client-request-id") == "request-1"
        body: dict[str, Any] = json.loads(cast(bytes, outbound.data))
        assert body == {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "generationConfig": {"maxOutputTokens": 100},
            "systemInstruction": {"parts": [{"text": "Be brief"}]},
        }
        assert "tools" not in body
        return HTTPResult(200, _gemini_payload())

    result = GeminiGenerateContentTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta", api_key="private-key", http=fake
    ).generate(_request("google/gemini-test"))
    assert (result.text, result.provider_response_id, result.input_tokens, result.output_tokens) == (
        "Answer",
        "resp_123",
        4,
        2,
    )
    assert result.finish_reason == "completed"


def test_gemini_accepts_discovered_model_name() -> None:
    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        assert outbound.full_url.endswith("/models/gemini-test:generateContent")
        return HTTPResult(200, _gemini_payload())

    assert _transport("gemini", fake).generate(_request("models/gemini-test")).text == "Answer"


def test_gemini_thinking_usage_includes_hidden_output_tokens() -> None:
    payload = _gemini_payload(
        usageMetadata={
            "promptTokenCount": 4,
            "candidatesTokenCount": 2,
            "thoughtsTokenCount": 8,
            "totalTokenCount": 14,
        }
    )
    result = _transport("gemini", lambda outbound, *, timeout: HTTPResult(200, payload)).generate(
        _request("gemini-test")
    )
    assert result.input_tokens == 4
    assert result.output_tokens == 10
    assert result.cost_usd_ticks is None


def test_gemini_rejects_inconsistent_total_token_usage() -> None:
    payload = _gemini_payload(
        usageMetadata={
            "promptTokenCount": 4,
            "candidatesTokenCount": 2,
            "thoughtsTokenCount": 8,
            "totalTokenCount": 6,
        }
    )
    with pytest.raises(ProviderError, match="usage"):
        _transport("gemini", lambda outbound, *, timeout: HTTPResult(200, payload)).generate(
            _request("gemini-test")
        )


@pytest.mark.parametrize(
    "status,error",
    [
        (400, InvalidRequestError),
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, InvalidRequestError),
        (422, InvalidRequestError),
        (429, RateLimitError),
        (500, ProviderError),
    ],
)
@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_native_http_errors_are_redacted(provider: str, status: int, error: type[Exception]) -> None:
    transport = _transport(
        provider, lambda outbound, *, timeout: HTTPResult(status, b"private-key diagnostic")
    )
    with pytest.raises(error) as raised:
        transport.generate(_request("test-model"))
    assert "private-key" not in str(raised.value)


def _transport(provider: str, http: Any) -> AnthropicMessagesTransport | GeminiGenerateContentTransport:
    if provider == "anthropic":
        return AnthropicMessagesTransport(
            endpoint="https://api.anthropic.com/v1", api_key="private-key", http=http
        )
    return GeminiGenerateContentTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta", api_key="private-key", http=http
    )


@pytest.mark.parametrize(
    "provider,payload,error",
    [
        ("anthropic", b"{broken", ProviderError),
        ("anthropic", _anthropic_payload(stop_reason="max_tokens"), IncompleteResponseError),
        ("anthropic", _anthropic_payload(content=[]), IncompleteResponseError),
        ("anthropic", _anthropic_payload(content=[{"type": "tool_use", "id": "x"}]), IncompleteResponseError),
        ("anthropic", _anthropic_payload(usage={"input_tokens": True, "output_tokens": 2}), ProviderError),
        ("gemini", b"{broken", ProviderError),
        ("gemini", _gemini_payload(candidates=[{"finishReason": "MAX_TOKENS"}]), IncompleteResponseError),
        ("gemini", _gemini_payload(candidates=[]), ProviderError),
        (
            "gemini",
            _gemini_payload(
                candidates=[{"finishReason": "STOP", "content": {"parts": [{"functionCall": {"name": "x"}}]}}]
            ),
            IncompleteResponseError,
        ),
        ("gemini", _gemini_payload(usageMetadata={"promptTokenCount": 1}), ProviderError),
    ],
)
def test_native_malformed_and_incomplete(provider: str, payload: bytes, error: type[Exception]) -> None:
    transport = _transport(provider, lambda outbound, *, timeout: HTTPResult(200, payload))
    with pytest.raises(error):
        transport.generate(_request("test-model"))


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_native_timeout_is_redacted(provider: str) -> None:
    def timeout(outbound: Request, *, timeout: float) -> HTTPResult:
        raise TimeoutError("private-key socket detail")

    with pytest.raises(TransportError) as raised:
        _transport(provider, timeout).generate(_request("test-model"))
    assert "private-key" not in str(raised.value)


@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("anthropic", "http://api.anthropic.com/v1"),
        ("anthropic", "https://api.anthropic.com/v1?key=secret"),
        ("gemini", "https://evil.example/v1beta"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/other"),
    ],
)
def test_native_rejects_untrusted_endpoint(provider: str, endpoint: str) -> None:
    with pytest.raises(InvalidRequestError):
        if provider == "anthropic":
            AnthropicMessagesTransport(endpoint=endpoint, api_key="private-key")
        else:
            GeminiGenerateContentTransport(endpoint=endpoint, api_key="private-key")


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_native_rejects_path_like_model(provider: str) -> None:
    with pytest.raises(InvalidRequestError):
        _transport(provider, lambda outbound, *, timeout: HTTPResult(200, b"{}")).generate(
            _request("../different-model")
        )


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_native_rejects_oversized_request_before_send(provider: str) -> None:
    def unexpected(outbound: Request, *, timeout: float) -> HTTPResult:
        pytest.fail("oversized request was sent")

    with pytest.raises(InvalidRequestError, match="size limit"):
        _transport(provider, unexpected).generate(
            TextRequest("test-model", "Be brief", "x" * 256_000, 100, 12, "request-1")
        )
