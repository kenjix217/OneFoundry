"""Bounded text calls for cataloged OpenAI-compatible API providers."""

from __future__ import annotations

import json
from urllib.request import Request

from onefoundry.provider_registry import provider_spec

from .contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
)
from .http import HTTPTransport, send_http


class CompatibleChatTransport:
    """One request to a fixed catalog endpoint; no arbitrary URL or tools."""

    def __init__(self, *, provider: str, api_key: str, http: HTTPTransport = send_http) -> None:
        spec = provider_spec(provider)
        if spec is None or not spec.connectable or spec.family != "openai_compatible" or not spec.base_url:
            raise InvalidRequestError("compatible provider is unavailable")
        if not api_key or any(char in api_key for char in "\r\n"):
            raise InvalidRequestError("provider credential is invalid")
        self._url = spec.base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        body = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": request.prompt},
            ],
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        encoded = json.dumps(body, separators=(",", ":")).encode()
        if len(encoded) > 256_000:
            raise InvalidRequestError("provider request exceeds size limit")
        outbound = Request(  # noqa: S310 - URL is from the validated bundled catalog
            self._url,
            data=encoded,
            headers={
                "authorization": f"Bearer {self._key}",
                "content-type": "application/json",
                "x-client-request-id": request.request_id,
            },
            method="POST",
        )
        response = self._http(outbound, timeout=request.timeout_seconds)
        if response.status in (401, 403):
            raise AuthenticationError("provider authentication failed")
        if response.status == 429:
            raise RateLimitError("provider rate limit reached")
        if response.status != 200:
            raise ProviderError(f"provider request failed with HTTP {response.status}")
        try:
            payload = json.loads(response.body)
            if not isinstance(payload, dict):
                raise ValueError()
            identifier = payload["id"]
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice["finish_reason"]
            usage = payload["usage"]
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
        except (ValueError, UnicodeDecodeError, TypeError, KeyError, IndexError):
            raise ProviderError("provider returned malformed completion") from None
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(content, str)
            or isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            raise ProviderError("provider returned malformed completion")
        if finish_reason != "stop" or not content.strip():
            raise IncompleteResponseError("provider response did not complete")
        return TextResult(content, identifier, input_tokens, output_tokens, "completed")
