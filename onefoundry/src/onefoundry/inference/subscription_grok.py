"""Bounded, text-only Grok subscription calls over the CLI streaming proxy."""

from __future__ import annotations

import json
from urllib.request import Request

from onefoundry.grok_account import valid_account_token, valid_model_id

from .contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InferenceError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
    TransportError,
)
from .http import MAX_RESPONSE_BYTES, HTTPResult, HTTPTransport, send_http
from .responses import _tokens

DEFAULT_ENDPOINT = "https://cli-chat-proxy.grok.com/v1/chat/completions"
_LEGACY_ENDPOINT = "https://cli-chat-proxy.grok.com/v1/responses"


def _decode_stream(response: HTTPResult) -> TextResult:
    if response.status in (401, 403):
        raise AuthenticationError("Grok account authentication failed")
    if response.status == 429:
        raise RateLimitError("Grok account rate limit reached")
    if response.status in (400, 422):
        raise InvalidRequestError("Grok account rejected the request")
    if response.status != 200:
        raise ProviderError(f"Grok account request failed with HTTP {response.status}")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ProviderError("Grok account response exceeded the size limit")
    parts: list[str] = []
    identifier = ""
    usage: dict[str, object] | None = None
    finished = False
    done = False
    try:
        events = response.body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
        for event in events:
            data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
            if not data:
                continue
            if done:
                raise ValueError()
            if data == "[DONE]":
                done = True
                continue
            chunk = json.loads(data)
            if not isinstance(chunk, dict) or "error" in chunk:
                raise ValueError()
            current_id = chunk.get("id")
            if not isinstance(current_id, str) or not 1 <= len(current_id) <= 256:
                raise ValueError()
            if identifier and current_id != identifier:
                raise ValueError()
            identifier = current_id
            choices = chunk.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError()
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index") != 0 or finished:
                    raise ValueError()
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    raise ValueError()
                if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                    raise IncompleteResponseError("Grok account returned no completed text reply")
                content = delta.get("content")
                if content is not None:
                    if not isinstance(content, str):
                        raise ValueError()
                    parts.append(content)
                reason = choice.get("finish_reason")
                if reason is not None:
                    if reason != "stop":
                        raise IncompleteResponseError("Grok account response did not complete")
                    finished = True
            if chunk.get("usage") is not None:
                if not isinstance(chunk["usage"], dict):
                    raise ValueError()
                usage = chunk["usage"]
    except (ValueError, UnicodeDecodeError, TypeError, KeyError):
        raise ProviderError("Grok account returned a malformed stream") from None
    text = "".join(parts)
    if not done or not finished or not text.strip():
        raise IncompleteResponseError("Grok account stream ended before completion")
    if usage is None:
        raise ProviderError("Grok account returned no usage")
    return TextResult(
        text, identifier, _tokens(usage, "prompt_tokens"), _tokens(usage, "completion_tokens"), "completed"
    )


class GrokAccountTransport:
    """One streamed text call; account connection and refresh belong to the gateway."""

    def __init__(self, *, endpoint: str, token: str, http: HTTPTransport = send_http) -> None:
        # The prototype constructor endpoint remains an alias for internal callers.
        if endpoint not in {DEFAULT_ENDPOINT, _LEGACY_ENDPOINT}:
            raise InvalidRequestError("Grok account endpoint is invalid")
        if not valid_account_token(token):
            raise InvalidRequestError("Grok account token is invalid")
        self._endpoint = DEFAULT_ENDPOINT
        self._token = token
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        model = request.model.removeprefix("grok_account/")
        if not valid_model_id(model):
            raise InvalidRequestError("Grok account model is invalid")
        if request.images:
            raise InvalidRequestError("image input is unavailable for this provider")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": request.prompt},
            ],
            "max_completion_tokens": request.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "store": False,
        }
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 256_000:
            raise InvalidRequestError("Grok account request exceeds size limit")
        outbound = Request(  # noqa: S310 - endpoint is exact-host validated
            self._endpoint,
            data=encoded,
            headers={
                "authorization": f"Bearer {self._token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-model-override": model,
                "content-type": "application/json",
                "x-client-request-id": request.request_id,
            },
            method="POST",
        )
        try:
            return _decode_stream(self._http(outbound, timeout=request.timeout_seconds))
        except InferenceError:
            raise
        except Exception:
            raise TransportError("Grok account transport failed") from None
