"""Offline wire-contract checks for dormant subscription transports."""

from __future__ import annotations

import json
from typing import cast
from urllib.request import Request

import pytest

from onefoundry.inference.contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InputImage,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TransportError,
)
from onefoundry.inference.http import HTTPResult, send_http_stream
from onefoundry.inference.subscription_codex import (
    CodexAccountTransport,
    CodexStreamFailure,
    CodexStreamIncomplete,
)
from onefoundry.inference.subscription_grok import GrokAccountTransport
from onefoundry.live_text_store import _diagnostic_code

TOKEN = "secret-subscription-token"
REQUEST = TextRequest("codex_account/test-model", "Stay on task.", "Hello", 128, 8.0, "req-1")


def _payload() -> dict[str, object]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello back"}]}],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def _codex_body(payload: dict[str, object] | None = None) -> bytes:
    event = {"type": "response.completed", "response": payload or _payload()}
    return f"data: {json.dumps(event)}\n\n".encode()


def test_codex_wire_and_completed_text() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        assert timeout == 8.0
        return HTTPResult(200, _codex_body())

    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=http,
    )
    result = transport.generate(REQUEST)
    assert (result.text, result.provider_response_id, result.input_tokens, result.output_tokens) == (
        "Hello back",
        "resp_1",
        4,
        2,
    )
    outbound = calls[0]
    assert outbound.get_header("Authorization") == f"Bearer {TOKEN}"
    assert outbound.get_header("Chatgpt-account-id") == "account-1"
    assert isinstance(outbound.data, bytes)
    body = json.loads(outbound.data)
    assert body == {
        "model": "test-model",
        "instructions": "Stay on task.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "store": False,
        "stream": True,
    }


def test_codex_sends_selected_reasoning_effort() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return HTTPResult(200, _codex_body())

    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=http,
    )
    transport.generate(TextRequest("test-model", "Guide", "Hello", 128, 8.0, "req-1", "high"))
    assert json.loads(cast(bytes, calls[0].data))["reasoning"] == {"effort": "high"}


def test_codex_stream_exposes_only_completed_public_summaries() -> None:
    updates: list[str] = []

    def stream(outbound: Request, *, timeout: float, on_event: object) -> HTTPResult:
        assert timeout == 8.0
        body = json.loads(cast(bytes, outbound.data))
        assert body["reasoning"] == {"summary": "auto"}
        assert callable(on_event)
        for event in (
            {"type": "response.reasoning_text.delta", "delta": "private text"},
            {"type": "response.reasoning_summary_text.delta", "delta": "partial"},
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": "rs1",
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": "I will inspect the work order."},
            },
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": "rs1",
                "output_index": 0,
                "summary_index": 0,
                "text": "I will inspect the work order.",
            },
        ):
            on_event(f"data: {json.dumps(event)}\n\n".encode())
        return HTTPResult(200, _codex_body())

    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http_stream=stream,
    )
    request = TextRequest("test-model", "Guide", "Hello", 128, 8.0, "req-1", progress_callback=updates.append)
    assert transport.generate(request).text == "Hello back"
    assert updates == ["I will inspect the work order."]


def test_http_stream_delivers_event_before_terminal_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []

    class Response:
        status = 200

        def __init__(self) -> None:
            self.lines = iter([b'data: {"type":"progress"}\n', b"\n", _codex_body()])

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def readline(self, _size: int) -> bytes:
            line = next(self.lines, b"")
            if line.startswith(b'data: {"type":"response.completed"'):
                assert seen == [b'data: {"type":"progress"}\n']
            return line

    class Opener:
        def open(self, _outbound: Request, *, timeout: float) -> Response:
            assert timeout == 8.0
            return Response()

    monkeypatch.setattr("onefoundry.inference.http.build_opener", lambda *_args: Opener())
    result = send_http_stream(Request("https://example.invalid"), timeout=8.0, on_event=seen.append)
    assert result.status == 200
    assert result.body.endswith(_codex_body())


def test_codex_sends_verified_image_part() -> None:
    calls: list[Request] = []

    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return HTTPResult(200, _codex_body())

    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=fake,
    )
    request = TextRequest(
        "test-model",
        "Guide",
        "See file",
        128,
        8.0,
        "req-1",
        images=(InputImage("image/png", b"image-bytes"),),
    )
    transport.generate(request)
    body = json.loads(cast(bytes, calls[0].data))
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "See file"},
        {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2UtYnl0ZXM="},
    ]


@pytest.mark.parametrize("status", [400, 422])
def test_codex_invalid_request_is_a_definite_rejection(status: int) -> None:
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(status, f"provider echoed {TOKEN}".encode()),
    )
    with pytest.raises(InvalidRequestError) as raised:
        transport.generate(REQUEST)
    assert TOKEN not in str(raised.value)


def test_grok_wire_and_completed_text() -> None:
    calls: list[Request] = []

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        assert timeout == 8.0
        return HTTPResult(200, _grok_body())

    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/responses",
        token=TOKEN,
        http=http,
    )
    result = transport.generate(TextRequest("grok_account/grok-test", "Guide", "Hello", 128, 8.0, "req-1"))
    assert (result.text, result.input_tokens, result.output_tokens) == ("Hello back", 4, 2)
    assert calls[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert calls[0].full_url == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert calls[0].get_header("X-xai-token-auth") == "xai-grok-cli"
    assert calls[0].get_header("X-grok-model-override") == "grok-test"
    assert isinstance(calls[0].data, bytes)
    assert json.loads(calls[0].data) == {
        "model": "grok-test",
        "messages": [{"role": "system", "content": "Guide"}, {"role": "user", "content": "Hello"}],
        "max_completion_tokens": 128,
        "stream": True,
        "stream_options": {"include_usage": True},
        "store": False,
    }


def _grok_body(*, finish_reason: str = "stop", done: bool = True) -> bytes:
    chunks = [
        {"id": "chat-test", "choices": [{"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}]},
        {
            "id": "chat-test",
            "choices": [{"index": 0, "delta": {"content": "back"}, "finish_reason": finish_reason}],
        },
        {"id": "chat-test", "choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
    ]
    return (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        + ("data: [DONE]\n\n" if done else "")
    ).encode()


@pytest.mark.parametrize("reason,done", [("length", True), ("stop", False)])
def test_grok_requires_complete_text_stream(reason: str, done: bool) -> None:
    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/chat/completions",
        token=TOKEN,
        http=lambda outbound, *, timeout: HTTPResult(200, _grok_body(finish_reason=reason, done=done)),
    )
    with pytest.raises(IncompleteResponseError):
        transport.generate(TextRequest("grok-test", "Guide", "Hello", 128, 8.0, "req-1"))


@pytest.mark.parametrize("invalid", ["model\nheader", "vendor/model", "bad model"])
def test_grok_rejects_unsafe_model_headers_without_egress(invalid: str) -> None:
    def no_call(outbound: Request, *, timeout: float) -> HTTPResult:
        pytest.fail("invalid model must be rejected before transport")

    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/chat/completions", token=TOKEN, http=no_call
    )
    with pytest.raises(InvalidRequestError):
        transport.generate(TextRequest(invalid, "Guide", "Hello", 128, 8.0, "req-1"))


def test_grok_text_route_rejects_image_without_egress() -> None:
    def no_call(outbound: Request, *, timeout: float) -> HTTPResult:
        pytest.fail("unsupported image must be rejected before transport")

    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/chat/completions", token=TOKEN, http=no_call
    )
    with pytest.raises(InvalidRequestError, match="image input"):
        transport.generate(
            TextRequest(
                "grok-test",
                "Guide",
                "Hello",
                128,
                8.0,
                "req-1",
                images=(InputImage("image/png", b"fixture"),),
            )
        )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"data: {}\n\n",
        b"data: [DONE]\n\n",
        _grok_body().replace(b'"prompt_tokens": 4', b'"prompt_tokens": true'),
    ],
)
def test_grok_empty_malformed_and_invalid_usage_streams_fail_closed(data: bytes) -> None:
    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/chat/completions",
        token=TOKEN,
        http=lambda outbound, *, timeout: HTTPResult(200, data),
    )
    with pytest.raises(ProviderError):
        transport.generate(TextRequest("grok-test", "Guide", "Hello", 128, 8.0, "req-1"))


@pytest.mark.parametrize("status,error", [(401, AuthenticationError), (429, RateLimitError)])
@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_subscription_http_errors_are_typed_and_redacted(
    status: int,
    error: type[Exception],
    provider: str,
) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        return HTTPResult(status, f"provider echoed {TOKEN}".encode())

    transport: CodexAccountTransport | GrokAccountTransport
    if provider == "codex":
        transport = CodexAccountTransport(
            endpoint="https://chatgpt.com/backend-api/codex/responses",
            token=TOKEN,
            account_id="account-1",
            http=http,
        )
        request = REQUEST
    else:
        transport = GrokAccountTransport(
            endpoint="https://cli-chat-proxy.grok.com/v1/responses",
            token=TOKEN,
            http=http,
        )
        request = TextRequest("grok_account/grok-test", "Guide", "Hello", 128, 8.0, "req-1")
    with pytest.raises(error) as raised:
        transport.generate(request)
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_subscription_rejects_malformed_response(provider: str) -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        return HTTPResult(200, b"not-json")

    transport: CodexAccountTransport | GrokAccountTransport
    if provider == "codex":
        transport = CodexAccountTransport(
            endpoint="https://chatgpt.com/backend-api/codex/responses",
            token=TOKEN,
            account_id="account-1",
            http=http,
        )
        request = REQUEST
    else:
        transport = GrokAccountTransport(
            endpoint="https://cli-chat-proxy.grok.com/v1/responses",
            token=TOKEN,
            http=http,
        )
        request = TextRequest("grok_account/grok-test", "Guide", "Hello", 128, 8.0, "req-1")
    with pytest.raises(ProviderError):
        transport.generate(request)


def test_codex_requires_terminal_event_and_fails_closed_on_tool_only_response() -> None:
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(
            200, b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        ),
    )
    with pytest.raises(IncompleteResponseError):
        transport.generate(REQUEST)


@pytest.mark.parametrize(
    ("body", "code", "error_type"),
    [
        (
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            "stream_ended_without_completion",
            CodexStreamIncomplete,
        ),
        (
            b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n',
            "response_incomplete",
            CodexStreamFailure,
        ),
        (
            b'data: {"type":"response.completed","response":{"id":"r","status":"completed",'
            b'"output":[],"usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
            "completed_without_text",
            CodexStreamFailure,
        ),
    ],
)
def test_codex_terminal_paths_keep_distinct_safe_codes(
    body: bytes, code: str, error_type: type[Exception]
) -> None:
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(200, body),
    )
    with pytest.raises(error_type) as raised:
        transport.generate(REQUEST)
    assert isinstance(raised.value, (CodexStreamFailure, CodexStreamIncomplete))
    assert raised.value.diagnostic_code == code
    assert _diagnostic_code(raised.value) == code
    assert TOKEN not in str(raised.value)


def test_codex_uses_text_deltas_only_after_confirmed_completion() -> None:
    payload = _payload()
    payload["output"] = []
    body = (
        'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
        'data: {"type":"response.output_text.delta","delta":"back"}\n\n'
        f"data: {json.dumps({'type': 'response.completed', 'response': payload})}\n\n"
    ).encode()
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(200, body),
    )
    assert transport.generate(REQUEST).text == "Hello back"


@pytest.mark.parametrize(
    ("provider_code", "diagnostic"),
    [
        ("subscription_sharing_usage_limit_exceeded", "response_failed_usage_limit"),
        ("subscription_sharing_usage_unavailable", "response_failed_usage_unavailable"),
        ("unknown_provider_code", "response_failed"),
    ],
)
def test_codex_failed_terminal_event_is_definite_and_redacted(provider_code: str, diagnostic: str) -> None:
    event = {"type": "response.failed", "response": {"error": {"code": provider_code, "message": TOKEN}}}
    body = f"data: {json.dumps(event)}\n\n".encode()
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(200, body),
    )
    with pytest.raises(CodexStreamFailure) as raised:
        transport.generate(REQUEST)
    assert _diagnostic_code(raised.value) == diagnostic
    assert TOKEN not in str(raised.value)


def test_codex_stream_error_event_is_redacted_failure() -> None:
    body = f"data: {json.dumps({'type': 'error', 'error': {'message': TOKEN}})}\n\n".encode()
    transport = CodexAccountTransport(
        endpoint="https://chatgpt.com/backend-api/codex/responses",
        token=TOKEN,
        account_id="account-1",
        http=lambda outbound, *, timeout: HTTPResult(200, body),
    )
    with pytest.raises(CodexStreamFailure) as raised:
        transport.generate(REQUEST)
    assert raised.value.diagnostic_code == "response_error"
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("codex", "https://evil.example/backend-api/codex/responses"),
        ("grok", "https://evil.example/v1/responses"),
    ],
)
def test_subscription_rejects_untrusted_endpoint(provider: str, endpoint: str) -> None:
    with pytest.raises(InvalidRequestError):
        if provider == "codex":
            CodexAccountTransport(endpoint=endpoint, token=TOKEN, account_id="account-1")
        else:
            GrokAccountTransport(endpoint=endpoint, token=TOKEN)


def test_transport_exception_does_not_echo_token() -> None:
    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        raise ValueError(f"bad header {TOKEN}")

    transport = GrokAccountTransport(
        endpoint="https://cli-chat-proxy.grok.com/v1/responses",
        token=TOKEN,
        http=http,
    )
    with pytest.raises(TransportError) as raised:
        transport.generate(TextRequest("grok_account/grok-test", "Guide", "Hello", 128, 8.0, "req-1"))
    assert TOKEN not in str(raised.value)
