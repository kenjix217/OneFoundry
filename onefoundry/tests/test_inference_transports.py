"""Offline contracts for opt-in cloud and local text transports."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, cast
from unittest.mock import patch
from urllib.request import Request

import pytest

from onefoundry.inference import (
    AuthenticationError,
    IncompleteResponseError,
    InvalidRequestError,
    OllamaTextTransport,
    OpenAIResponsesTransport,
    ProviderError,
    RateLimitError,
    TextRequest,
    TransportError,
    XAIResponsesTransport,
)
from onefoundry.inference.contracts import InputImage
from onefoundry.inference.http import HTTPResult, send_http


def request() -> TextRequest:
    return TextRequest("example-model", "be concise", "Hello", 100, 12, "request-1")


def cloud_body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "id": "resp_123",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Answer"}]}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    ("factory", "endpoint", "host"),
    [
        (OpenAIResponsesTransport, "https://api.openai.com/v1", "api.openai.com"),
        (XAIResponsesTransport, "https://api.x.ai/v1", "api.x.ai"),
    ],
)
def test_cloud_success_is_bounded_and_tool_free(
    factory: type[OpenAIResponsesTransport] | type[XAIResponsesTransport], endpoint: str, host: str
) -> None:
    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        assert outbound.full_url == f"https://{host}/v1/responses"
        assert outbound.get_header("Authorization") == "Bearer private-key"
        assert outbound.get_header("X-client-request-id") == "request-1"
        assert timeout == 12
        body = json.loads(cast(bytes, outbound.data))
        assert body == {
            "model": "example-model",
            "instructions": "be concise",
            "input": "Hello",
            "max_output_tokens": 100,
            "store": False,
        }
        assert "tools" not in body
        return HTTPResult(200, cloud_body())

    result = factory(endpoint=endpoint, api_key="private-key", http=fake).generate(request())
    assert (result.text, result.provider_response_id, result.input_tokens, result.output_tokens) == (
        "Answer",
        "resp_123",
        4,
        2,
    )
    assert result.cost_usd_ticks is None


def test_xai_exact_billed_cost_ticks_are_preserved() -> None:
    transport = XAIResponsesTransport(
        endpoint="https://api.x.ai/v1",
        api_key="private-key",
        http=lambda outbound, *, timeout: HTTPResult(
            200, cloud_body(usage={"input_tokens": 4, "output_tokens": 2, "cost_in_usd_ticks": 158500})
        ),
    )
    assert transport.generate(request()).cost_usd_ticks == 158500


def test_openai_image_wire_and_xai_rejection_before_egress() -> None:
    calls: list[Request] = []

    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        return HTTPResult(200, cloud_body())

    visual = TextRequest(
        "example-model",
        "Guide",
        "Describe this",
        100,
        12,
        "visual-1",
        images=(InputImage("image/png", b"sample"),),
    )
    OpenAIResponsesTransport(endpoint="https://api.openai.com/v1", api_key="private-key", http=fake).generate(
        visual
    )
    body = json.loads(cast(bytes, calls[0].data))
    assert body["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this"},
                {"type": "input_image", "image_url": "data:image/png;base64,c2FtcGxl"},
            ],
        }
    ]
    with pytest.raises(InvalidRequestError, match="image input"):
        XAIResponsesTransport(endpoint="https://api.x.ai/v1", api_key="private-key", http=fake).generate(
            visual
        )
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [True, -1, 1.5, "100", None])
def test_xai_rejects_malformed_billed_cost(bad: object) -> None:
    transport = XAIResponsesTransport(
        endpoint="https://api.x.ai/v1",
        api_key="private-key",
        http=lambda outbound, *, timeout: HTTPResult(
            200, cloud_body(usage={"input_tokens": 4, "output_tokens": 2, "cost_in_usd_ticks": bad})
        ),
    )
    with pytest.raises(ProviderError, match="malformed billed cost"):
        transport.generate(request())


def test_openai_ignores_xai_specific_cost_field() -> None:
    transport = OpenAIResponsesTransport(
        endpoint="https://api.openai.com/v1",
        api_key="private-key",
        http=lambda outbound, *, timeout: HTTPResult(
            200, cloud_body(usage={"input_tokens": 4, "output_tokens": 2, "cost_in_usd_ticks": "irrelevant"})
        ),
    )
    assert transport.generate(request()).cost_usd_ticks is None


@pytest.mark.parametrize(
    "status,error", [(401, AuthenticationError), (429, RateLimitError), (500, ProviderError)]
)
def test_cloud_errors_never_expose_provider_body(status: int, error: type[Exception]) -> None:
    transport = OpenAIResponsesTransport(
        endpoint="https://api.openai.com/v1",
        api_key="private-key",
        http=lambda outbound, *, timeout: HTTPResult(status, b"private-key provider diagnostic"),
    )
    with pytest.raises(error) as raised:
        transport.generate(request())
    assert "private-key" not in str(raised.value)


@pytest.mark.parametrize(
    "payload", [b"{bad", cloud_body(status="incomplete"), cloud_body(output=[]), cloud_body(usage={})]
)
def test_cloud_malformed_or_incomplete_fails(payload: bytes) -> None:
    transport = OpenAIResponsesTransport(
        endpoint="https://api.openai.com/v1",
        api_key="private-key",
        http=lambda outbound, *, timeout: HTTPResult(200, payload),
    )
    with pytest.raises((ProviderError, IncompleteResponseError)):
        transport.generate(request())


def test_timeout_is_redacted() -> None:
    def timeout(outbound: Request, *, timeout: float) -> HTTPResult:
        raise TimeoutError("private-key from socket")

    transport = XAIResponsesTransport(endpoint="https://api.x.ai/v1", api_key="private-key", http=timeout)
    with pytest.raises(ProviderError) as raised:
        transport.generate(request())
    assert "private-key" not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint", ["http://api.openai.com/v1", "https://evil.example/v1", "https://api.openai.com/v1?key=x"]
)
def test_cloud_endpoint_rejects_untrusted_destinations(endpoint: str) -> None:
    with pytest.raises(InvalidRequestError):
        OpenAIResponsesTransport(endpoint=endpoint, api_key="secret")


def test_ollama_success_uses_local_generation_and_bound() -> None:
    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        assert outbound.full_url == "http://127.0.0.1:11434/api/generate"
        assert timeout == 12
        body: dict[str, Any] = json.loads(cast(bytes, outbound.data))
        assert body == {
            "model": "example-model",
            "prompt": "Hello",
            "system": "be concise",
            "stream": False,
            "options": {"num_predict": 100},
        }
        return HTTPResult(
            200,
            json.dumps(
                {
                    "response": "Local",
                    "done": True,
                    "created_at": "timestamp",
                    "prompt_eval_count": 3,
                    "eval_count": 1,
                    "done_reason": "stop",
                }
            ).encode(),
        )

    result = OllamaTextTransport(endpoint="http://127.0.0.1:11434", http=fake).generate(request())
    assert (result.text, result.input_tokens, result.output_tokens, result.finish_reason) == (
        "Local",
        3,
        1,
        "stop",
    )


def test_ollama_accepts_fixed_compose_service_name() -> None:
    transport = OllamaTextTransport(
        endpoint="http://ollama:11434",
        http=lambda outbound, *, timeout: HTTPResult(
            200,
            b'{"response":"Local","done":true,"created_at":"now",'
            b'"prompt_eval_count":1,"eval_count":1,"done_reason":"stop"}',
        ),
    )
    assert transport.generate(request()).text == "Local"


@pytest.mark.parametrize(
    "endpoint", ["http://example.com:11434", "https://localhost:11434", "http://localhost:11434/?x=1"]
)
def test_ollama_rejects_nonlocal_or_modified_endpoint(endpoint: str) -> None:
    with pytest.raises(InvalidRequestError):
        OllamaTextTransport(endpoint=endpoint)


@pytest.mark.parametrize(
    "payload", [b"bad", b'{"done": false, "response": "partial"}', b'{"done": true, "response": ""}']
)
def test_ollama_malformed_or_incomplete(payload: bytes) -> None:
    transport = OllamaTextTransport(
        endpoint="http://localhost:11434", http=lambda outbound, *, timeout: HTTPResult(200, payload)
    )
    with pytest.raises(ProviderError):
        transport.generate(request())


def test_ollama_length_finish_is_incomplete() -> None:
    payload = {
        "response": "Truncated",
        "done": True,
        "created_at": "now",
        "prompt_eval_count": 3,
        "eval_count": 100,
        "done_reason": "length",
    }
    transport = OllamaTextTransport(
        endpoint="http://localhost:11434",
        http=lambda outbound, *, timeout: HTTPResult(200, json.dumps(payload).encode()),
    )
    with pytest.raises(IncompleteResponseError):
        transport.generate(request())


@pytest.mark.parametrize("missing", ["prompt_eval_count", "eval_count"])
def test_ollama_missing_usage_is_provider_error(missing: str) -> None:
    payload = {
        "response": "Answer",
        "done": True,
        "created_at": "now",
        "prompt_eval_count": 3,
        "eval_count": 2,
        "done_reason": "stop",
    }
    del payload[missing]
    transport = OllamaTextTransport(
        endpoint="http://localhost:11434",
        http=lambda outbound, *, timeout: HTTPResult(200, json.dumps(payload).encode()),
    )
    with pytest.raises(ProviderError, match="malformed usage"):
        transport.generate(request())


def test_http_absolute_deadline_interrupts_slow_drip_without_retry() -> None:
    closed = threading.Event()

    class SlowResponse:
        status = 200

        def __enter__(self) -> SlowResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            self.close()

        def read(self, size: int) -> bytes:
            while not closed.wait(0.01):
                pass  # A byte could arrive on each interval; the body never completes.
            return b"partial"

        def close(self) -> None:
            closed.set()

    class SlowOpener:
        calls = 0

        def open(self, outbound: Request, timeout: float) -> SlowResponse:
            self.calls += 1
            return SlowResponse()

    opener = SlowOpener()
    outbound = Request("http://127.0.0.1:11434/api/generate", method="POST")
    with patch("onefoundry.inference.http.build_opener", return_value=opener):
        started = time.monotonic()
        with pytest.raises(TransportError, match="deadline exceeded"):
            send_http(outbound, timeout=0.05)
        elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert closed.wait(0.5)
    assert opener.calls == 1


def test_http_worker_start_failure_releases_transport_capacity() -> None:
    outbound = Request("http://127.0.0.1:11434/api/generate", method="POST")
    with patch("onefoundry.inference.http.threading.Thread.start", side_effect=RuntimeError):
        for _ in range(16):
            with pytest.raises(TransportError, match="unavailable"):
                send_http(outbound, timeout=0.05)

    class FastResponse:
        status = 200

        def __enter__(self) -> FastResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return b"ok"

        def close(self) -> None:
            return None

    class FastOpener:
        def open(self, request: Request, timeout: float) -> FastResponse:
            return FastResponse()

    with patch("onefoundry.inference.http.build_opener", return_value=FastOpener()):
        assert send_http(outbound, timeout=0.2).body == b"ok"
