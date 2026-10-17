"""One-shot text calls to native Anthropic Messages and Gemini GenerateContent APIs."""

from __future__ import annotations

import json
import re
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
from .http import HTTPResult, HTTPTransport, send_http

_MAX_REQUEST_BYTES = 256_000
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def _endpoint(endpoint: str, *, host: str, path: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise InvalidRequestError("provider endpoint is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != path
    ):
        raise InvalidRequestError("provider endpoint is invalid")
    return f"https://{host}{path}"


def _credential(api_key: str) -> str:
    if not api_key or any(char in api_key for char in "\r\n"):
        raise InvalidRequestError("provider credential is invalid")
    return api_key


def _model(model: str, *prefixes: str) -> str:
    for prefix in prefixes:
        if model.startswith(prefix):
            model = model[len(prefix) :]
            break
    if _MODEL_NAME.fullmatch(model) is None:
        raise InvalidRequestError("provider model is invalid")
    return model


def _post(
    http: HTTPTransport,
    url: str,
    key_header: str,
    key: str,
    body: dict[str, Any],
    request: TextRequest,
    *,
    version: str | None = None,
) -> HTTPResult:
    encoded = json.dumps(body, separators=(",", ":")).encode()
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise InvalidRequestError("provider request exceeds size limit")
    headers = {key_header: key, "content-type": "application/json", "x-client-request-id": request.request_id}
    if version is not None:
        headers["anthropic-version"] = version
    outbound = Request(url, data=encoded, headers=headers, method="POST")  # noqa: S310 - fixed HTTPS host
    try:
        response = http(outbound, timeout=request.timeout_seconds)
    except TransportError:
        raise
    except (OSError, TimeoutError, ValueError):
        raise TransportError("provider transport failed") from None
    if response.status in (401, 403):
        raise AuthenticationError("provider authentication failed")
    if response.status == 429:
        raise RateLimitError("provider rate limit reached")
    if response.status in (400, 404, 422):
        raise InvalidRequestError("provider rejected the model or request")
    if response.status != 200:
        raise ProviderError(f"provider request failed with HTTP {response.status}")
    return response


def _payload(response: HTTPResult) -> dict[str, Any]:
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise ProviderError("provider returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ProviderError("provider returned malformed response")
    return value


def _tokens(usage: dict[str, Any], field: str) -> int:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("provider returned malformed usage")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderError("provider returned malformed response id")
    return value


class AnthropicMessagesTransport:
    """Generate text through Anthropic's nonstreaming Messages endpoint."""

    def __init__(self, *, endpoint: str, api_key: str, http: HTTPTransport = send_http) -> None:
        self._url = _endpoint(endpoint, host="api.anthropic.com", path="/v1") + "/messages"
        self._key = _credential(api_key)
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        body: dict[str, Any] = {
            "model": _model(request.model, "anthropic/"),
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.instructions:
            body["system"] = request.instructions
        payload = _payload(
            _post(
                self._http,
                self._url,
                "x-api-key",
                self._key,
                body,
                request,
                version="2023-06-01",
            )
        )
        if payload.get("stop_reason") != "end_turn":
            raise IncompleteResponseError("provider response did not complete")
        blocks = payload.get("content")
        usage = payload.get("usage")
        if not isinstance(blocks, list) or not isinstance(usage, dict):
            raise ProviderError("provider returned malformed response")
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ProviderError("provider returned malformed content")
            if block.get("type") == "tool_use":
                raise IncompleteResponseError("provider requested a tool")
            if block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ProviderError("provider returned malformed content")
                parts.append(text)
        text = "".join(parts)
        if not text.strip():
            raise IncompleteResponseError("provider returned no text")
        return TextResult(
            text,
            _identifier(payload.get("id")),
            _tokens(usage, "input_tokens"),
            _tokens(usage, "output_tokens"),
            "completed",
        )


class GeminiGenerateContentTransport:
    """Generate text through Google's nonstreaming GenerateContent endpoint."""

    def __init__(self, *, endpoint: str, api_key: str, http: HTTPTransport = send_http) -> None:
        self._base_url = _endpoint(endpoint, host="generativelanguage.googleapis.com", path="/v1beta")
        self._key = _credential(api_key)
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        model = _model(request.model, "google/", "gemini/", "models/")
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": {"maxOutputTokens": request.max_output_tokens},
        }
        if request.instructions:
            body["systemInstruction"] = {"parts": [{"text": request.instructions}]}
        payload = _payload(
            _post(
                self._http,
                f"{self._base_url}/models/{model}:generateContent",
                "x-goog-api-key",
                self._key,
                body,
                request,
            )
        )
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise ProviderError("provider returned malformed candidates")
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP":
            raise IncompleteResponseError("provider response did not complete")
        content = candidate.get("content")
        usage = payload.get("usageMetadata")
        if (
            not isinstance(content, dict)
            or not isinstance(content.get("parts"), list)
            or not isinstance(usage, dict)
        ):
            raise ProviderError("provider returned malformed response")
        parts: list[str] = []
        for part in content["parts"]:
            if not isinstance(part, dict):
                raise ProviderError("provider returned malformed content")
            if "functionCall" in part:
                raise IncompleteResponseError("provider requested a tool")
            if "text" in part:
                if not isinstance(part["text"], str):
                    raise ProviderError("provider returned malformed content")
                parts.append(part["text"])
        text = "".join(parts)
        if not text.strip():
            raise IncompleteResponseError("provider returned no text")
        prompt_tokens = _tokens(usage, "promptTokenCount")
        candidate_tokens = _tokens(usage, "candidatesTokenCount")
        total = usage.get("totalTokenCount")
        thoughts = usage.get("thoughtsTokenCount", 0)
        if isinstance(thoughts, bool) or not isinstance(thoughts, int) or thoughts < 0:
            raise ProviderError("provider returned malformed usage")
        if total is not None:
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < prompt_tokens + candidate_tokens + thoughts
            ):
                raise ProviderError("provider returned malformed usage")
            output_tokens = total - prompt_tokens
        else:
            output_tokens = candidate_tokens + thoughts
        return TextResult(
            text,
            _identifier(payload.get("responseId")),
            prompt_tokens,
            output_tokens,
            "completed",
        )
