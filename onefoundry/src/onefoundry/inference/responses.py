"""Explicit API-key text generation over official Responses endpoints."""

from __future__ import annotations

import json
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
)
from .http import HTTPResult, HTTPTransport, send_http


def _cloud_endpoint(endpoint: str, hostname: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise InvalidRequestError("provider endpoint is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise InvalidRequestError("provider endpoint is invalid")
    return f"https://{hostname}/v1/responses"


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("provider returned malformed response")
    return value


def _tokens(usage: dict[str, Any], name: str) -> int:
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("provider returned malformed usage")
    return value


def _decode_response(response: HTTPResult, *, xai_cost: bool = False) -> TextResult:
    if response.status in (401, 403):
        raise AuthenticationError("provider authentication failed")
    if response.status == 429:
        raise RateLimitError("provider rate limit reached")
    if response.status != 200:
        raise ProviderError(f"provider request failed with HTTP {response.status}")
    try:
        payload = _object(json.loads(response.body))
        if payload.get("status") != "completed":
            raise IncompleteResponseError("provider response did not complete")
        items = payload.get("output")
        if not isinstance(items, list):
            raise ProviderError("provider returned malformed output")
        parts: list[str] = []
        for item in items:
            message = _object(item)
            if message.get("type") != "message":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                raise ProviderError("provider returned malformed output")
            for part in content:
                value = _object(part)
                if value.get("type") == "refusal":
                    raise IncompleteResponseError("provider refused the request")
                if value.get("type") == "output_text":
                    text = value.get("text")
                    if not isinstance(text, str):
                        raise ProviderError("provider returned malformed output")
                    parts.append(text)
        text = "".join(parts)
        if not text.strip():
            raise IncompleteResponseError("provider returned no text")
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ProviderError("provider returned malformed response id")
        usage = _object(payload.get("usage"))
        cost_ticks: int | None = None
        if xai_cost and "cost_in_usd_ticks" in usage:
            raw_cost = usage["cost_in_usd_ticks"]
            if isinstance(raw_cost, bool) or not isinstance(raw_cost, int) or raw_cost < 0:
                raise ProviderError("provider returned malformed billed cost")
            cost_ticks = raw_cost
        return TextResult(
            text,
            identifier,
            _tokens(usage, "input_tokens"),
            _tokens(usage, "output_tokens"),
            "completed",
            cost_ticks,
        )
    except (ValueError, UnicodeDecodeError, TypeError):
        raise ProviderError("provider returned invalid JSON") from None


class _ResponsesTransport:
    def __init__(
        self, *, endpoint: str, api_key: str, hostname: str, http: HTTPTransport = send_http
    ) -> None:
        self._url = _cloud_endpoint(endpoint, hostname)
        self._xai_cost = hostname == "api.x.ai"
        if not api_key or any(char in api_key for char in "\r\n"):
            raise InvalidRequestError("provider credential is invalid")
        self._api_key = api_key
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        if self._xai_cost and request.images:
            raise InvalidRequestError("image input is unavailable for this provider")
        body = {
            "model": request.model,
            "instructions": request.instructions,
            "input": request.prompt,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.images:
            body["input"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": request.prompt},
                        *({"type": "input_image", "image_url": image.data_url()} for image in request.images),
                    ],
                }
            ]
        encoded = json.dumps(body, separators=(",", ":")).encode()
        limit = 12_000_000 if request.images else 256_000
        if len(encoded) > limit:
            raise InvalidRequestError("provider request exceeds size limit")
        outbound = Request(  # noqa: S310 - URL validated against an official HTTPS host
            self._url,
            data=encoded,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
                "x-client-request-id": request.request_id,
            },
            method="POST",
        )
        try:
            return _decode_response(
                self._http(outbound, timeout=request.timeout_seconds), xai_cost=self._xai_cost
            )
        except (AuthenticationError, RateLimitError, ProviderError, InvalidRequestError):
            raise
        except (OSError, TimeoutError, ValueError):
            raise ProviderError("provider transport failed") from None


class OpenAIResponsesTransport(_ResponsesTransport):
    def __init__(self, *, endpoint: str, api_key: str, http: HTTPTransport = send_http) -> None:
        super().__init__(endpoint=endpoint, api_key=api_key, hostname="api.openai.com", http=http)


class XAIResponsesTransport(_ResponsesTransport):
    def __init__(self, *, endpoint: str, api_key: str, http: HTTPTransport = send_http) -> None:
        super().__init__(endpoint=endpoint, api_key=api_key, hostname="api.x.ai", http=http)
