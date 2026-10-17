"""Injected-credential Codex account Responses transport.

OneFoundry's model gateway owns account login and refresh. This transport sees
only the current access token for a single bounded text request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from .contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
    TransportError,
)
from .http import HTTPResult, HTTPTransport, send_http, send_http_stream

DEFAULT_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"


class CodexStreamIncomplete(IncompleteResponseError):
    """A stream with no usable completed text, identified without provider content."""

    def __init__(self, diagnostic_code: str) -> None:
        super().__init__("Codex response did not produce completed text")
        self.diagnostic_code = diagnostic_code


class CodexStreamFailure(ProviderError):
    """The provider explicitly reported a failed response."""

    def __init__(self, diagnostic_code: str) -> None:
        super().__init__("Codex response failed")
        self.diagnostic_code = diagnostic_code


def _endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/backend-api/codex/responses"
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidRequestError("Codex account endpoint is invalid")
    return DEFAULT_ENDPOINT


def _credential(value: str, name: str) -> str:
    if not value or len(value) > 16_384 or any(char in value for char in "\r\n"):
        raise InvalidRequestError(f"Codex {name} is invalid")
    return value


def _sse_completed(body: bytes) -> tuple[dict[str, Any], str]:
    try:
        source = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderError("Codex returned invalid event encoding") from None
    completed: dict[str, Any] | None = None
    text_deltas: list[str] = []
    for block in source.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            raise ProviderError("Codex returned malformed event JSON") from None
        if not isinstance(event, dict):
            raise ProviderError("Codex returned malformed event")
        event_type = event.get("type")
        if event_type == "error" or event.get("error") is not None:
            raise CodexStreamFailure("response_error")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ProviderError("Codex returned malformed text delta")
            text_deltas.append(delta)
        if event_type == "response.failed":
            response = event.get("response")
            error = response.get("error") if isinstance(response, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            known_codes = {
                "subscription_sharing_usage_limit_exceeded": "response_failed_usage_limit",
                "subscription_sharing_usage_unavailable": "response_failed_usage_unavailable",
            }
            diagnostic = (
                known_codes.get(code, "response_failed") if isinstance(code, str) else "response_failed"
            )
            raise CodexStreamFailure(diagnostic)
        if event_type == "response.incomplete":
            raise CodexStreamFailure("response_incomplete")
        if event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict):
                raise ProviderError("Codex returned malformed completion")
            completed = response
    if completed is None:
        raise CodexStreamIncomplete("stream_ended_without_completion")
    return completed, "".join(text_deltas)


def _result(response: HTTPResult) -> TextResult:
    if response.status in (401, 403):
        raise AuthenticationError("Codex account authentication failed")
    if response.status == 429:
        raise RateLimitError("Codex account usage limit reached")
    if response.status in (400, 422):
        raise InvalidRequestError(f"Codex account rejected the request with HTTP {response.status}")
    if response.status != 200:
        raise ProviderError(f"Codex account request failed with HTTP {response.status}")
    payload, text_deltas = _sse_completed(response.body)
    if payload.get("status") != "completed":
        raise CodexStreamIncomplete("completion_status_unconfirmed")
    identifier = payload.get("id")
    output = payload.get("output")
    usage = payload.get("usage")
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(output, list)
        or not isinstance(usage, dict)
    ):
        raise ProviderError("Codex returned malformed completion")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise ProviderError("Codex returned malformed output")
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ProviderError("Codex returned malformed output")
        for part in content:
            if not isinstance(part, dict):
                raise ProviderError("Codex returned malformed output")
            if part.get("type") == "refusal":
                raise CodexStreamFailure("response_refusal")
            if part.get("type") == "output_text":
                value = part.get("text")
                if not isinstance(value, str):
                    raise ProviderError("Codex returned malformed output")
                parts.append(value)
    result_text = "".join(parts) or text_deltas
    if not result_text.strip():
        raise CodexStreamFailure("completed_without_text")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise ProviderError("Codex returned malformed usage")
    return TextResult(result_text, identifier, input_tokens, output_tokens, "completed")


class CodexAccountTransport:
    """One text-only call to the ChatGPT Codex Responses account endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        account_id: str,
        http: HTTPTransport = send_http,
        http_stream: Callable[..., HTTPResult] = send_http_stream,
    ) -> None:
        self._endpoint = _endpoint(endpoint)
        self._token = _credential(token, "token")
        self._account_id = _credential(account_id, "account id")
        self._http = http
        self._http_stream = http_stream

    def generate(self, request: TextRequest) -> TextResult:
        if not request.instructions.strip():
            raise InvalidRequestError("Codex instructions are required")
        model = request.model.removeprefix("codex_account/")
        if not model or "/" in model:
            raise InvalidRequestError("Codex model is invalid")
        body = {
            "model": model,
            "instructions": request.instructions,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.prompt}]
                    + [{"type": "input_image", "image_url": image.data_url()} for image in request.images],
                }
            ],
            "store": False,
            "stream": True,
        }
        reasoning: dict[str, str] = {}
        if request.reasoning_effort is not None:
            reasoning["effort"] = request.reasoning_effort
        if request.progress_callback is not None:
            reasoning["summary"] = "auto"
        if reasoning:
            body["reasoning"] = reasoning
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(encoded) > (12_000_000 if request.images else 256_000):
            raise InvalidRequestError("Codex request exceeds size limit")
        outbound = Request(  # noqa: S310 - endpoint is exact-host validated
            self._endpoint,
            data=encoded,
            headers={
                "authorization": f"Bearer {self._token}",
                "chatgpt-account-id": self._account_id,
                "content-type": "application/json",
                "accept": "text/event-stream",
                "openai-beta": "responses=experimental",
                "originator": "onefoundry",
                "x-client-request-id": request.request_id,
            },
            method="POST",
        )
        try:
            progress_callback = request.progress_callback
            if progress_callback is None:
                return _result(self._http(outbound, timeout=request.timeout_seconds))
            seen: set[tuple[object, object, object]] = set()

            def public_summary(block: bytes) -> None:
                data = b"\n".join(
                    line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:")
                )
                try:
                    event = json.loads(data)
                except (UnicodeError, ValueError):
                    return  # The terminal parser reports malformed provider events.
                if not isinstance(event, dict):
                    return
                kind = event.get("type")
                if kind == "response.reasoning_summary_part.done":
                    part = event.get("part")
                    summary = part.get("text") if isinstance(part, dict) else None
                elif kind == "response.reasoning_summary_text.done":
                    summary = event.get("text")
                else:
                    return
                key = (event.get("item_id"), event.get("output_index"), event.get("summary_index"))
                if key in seen or len(seen) >= 8 or not isinstance(summary, str) or not summary.strip():
                    return
                seen.add(key)
                progress_callback(summary)

            return _result(
                self._http_stream(outbound, timeout=request.timeout_seconds, on_event=public_summary)
            )
        except (AuthenticationError, RateLimitError, ProviderError, InvalidRequestError, TransportError):
            raise
        except (OSError, TimeoutError, ValueError):
            raise TransportError("Codex account transport failed") from None
