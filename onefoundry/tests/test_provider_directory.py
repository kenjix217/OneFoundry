"""Connector directory and generic transport behavior without provider egress."""

import json
from typing import cast
from unittest.mock import patch
from urllib.request import Request

import pytest

from onefoundry.inference import AuthenticationError, CompatibleChatTransport, TextRequest
from onefoundry.inference.contracts import IncompleteResponseError, InvalidRequestError, ProviderError
from onefoundry.inference.http import HTTPResult
from onefoundry.model_providers import discover_models
from onefoundry.provider_registry import provider_directory
from onefoundry.settings import Settings


def test_districtai_directory_and_account_aliases_have_explicit_capabilities() -> None:
    entries = provider_directory()
    assert len(entries) == 61
    assert len({entry.id for entry in entries}) == 61
    by_id = {entry.id: entry for entry in entries}
    assert by_id["openrouter"].connectable
    assert not by_id["bedrock"].connectable
    assert by_id["gemini"].catalog_supported
    assert by_id["grok"].text_runtime_available
    assert by_id["anthropic"].text_runtime_available
    assert by_id["gemini"].text_runtime_available
    assert by_id["xai"].kind == "account_runtime"
    assert by_id["xai"].text_runtime_available
    assert by_id["openai_codex_runtime"].connectable
    assert by_id["openai_codex_runtime"].text_runtime_available


def test_compatible_discovery_uses_catalog_host_and_never_puts_key_in_url() -> None:
    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return json.dumps({"data": [{"id": "example-chat", "object": "model"}]}).encode()

    class FakeOpener:
        def open(self, request: Request, timeout: int) -> FakeResponse:
            assert request.full_url == "https://openrouter.ai/api/v1/models"
            assert request.get_header("Authorization") == "Bearer private-key"
            assert timeout == 5
            return FakeResponse()

    with patch("onefoundry.model_providers.build_opener", return_value=FakeOpener()):
        found = discover_models("openrouter", "private-key", Settings(db_password="test-value"))
    assert [(item.model_id, item.category) for item in found] == [("example-chat", "text")]


def _request() -> TextRequest:
    return TextRequest("example-chat", "Respond briefly", "Private prompt", 120, 8, "req-1")


def test_compatible_chat_sends_one_bounded_tool_free_request_and_records_usage() -> None:
    calls: list[Request] = []

    def fake(outbound: Request, *, timeout: float) -> HTTPResult:
        calls.append(outbound)
        assert outbound.full_url == "https://openrouter.ai/api/v1/chat/completions"
        assert outbound.get_header("Authorization") == "Bearer private-key"
        assert timeout == 8
        body = json.loads(cast(bytes, outbound.data))
        assert body["messages"] == [
            {"role": "system", "content": "Respond briefly"},
            {"role": "user", "content": "Private prompt"},
        ]
        assert "tools" not in body and body["stream"] is False
        return HTTPResult(
            200,
            json.dumps(
                {
                    "id": "chat-1",
                    "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
            ).encode(),
        )

    result = CompatibleChatTransport(provider="openrouter", api_key="private-key", http=fake).generate(
        _request()
    )
    assert len(calls) == 1
    assert (result.text, result.input_tokens, result.output_tokens) == ("Answer", 12, 4)
    assert result.cost_usd_ticks is None


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, b"private-key", AuthenticationError),
        (200, b"private-key", ProviderError),
        (
            200,
            json.dumps(
                {
                    "id": "1",
                    "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }
            ).encode(),
            IncompleteResponseError,
        ),
    ],
)
def test_compatible_chat_rejects_errors_without_leaking_provider_body(
    status: int, payload: bytes, error: type[Exception]
) -> None:
    transport = CompatibleChatTransport(
        provider="openrouter",
        api_key="private-key",
        http=lambda _request, *, timeout: HTTPResult(status, payload),
    )
    with pytest.raises(error) as raised:
        transport.generate(_request())
    assert "private-key" not in str(raised.value)


def test_compatible_chat_rejects_unwired_provider() -> None:
    with pytest.raises(InvalidRequestError):
        CompatibleChatTransport(provider="bedrock", api_key="private-key")
