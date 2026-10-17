"""Local Ollama text generation adapter."""

from __future__ import annotations

import json
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from .contracts import IncompleteResponseError, InvalidRequestError, ProviderError, TextRequest, TextResult
from .http import HTTPTransport, send_http


def _local_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise InvalidRequestError("Ollama endpoint must be a local HTTP address") from None
    hostname = parsed.hostname
    try:
        loopback = hostname in {"localhost", "ollama"} or (
            hostname is not None and ip_address(hostname).is_loopback
        )
    except ValueError:
        loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise InvalidRequestError("Ollama endpoint must be a local HTTP address")
    host = f"[{hostname}]" if hostname and ":" in hostname else hostname
    return f"http://{host}:{port}/api/generate"


def _usage_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("local provider returned malformed usage")
    return value


class OllamaTextTransport:
    def __init__(self, *, endpoint: str, http: HTTPTransport = send_http) -> None:
        self._url = _local_endpoint(endpoint)
        self._http = http

    def generate(self, request: TextRequest) -> TextResult:
        body = {
            "model": request.model,
            "prompt": request.prompt,
            "system": request.instructions,
            "stream": False,
            "options": {"num_predict": request.max_output_tokens},
        }
        outbound = Request(  # noqa: S310 - URL validated as loopback HTTP
            self._url,
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"content-type": "application/json", "x-request-id": request.request_id},
            method="POST",
        )
        if len(json.dumps(body).encode()) > 256_000:
            raise InvalidRequestError("local provider request exceeds size limit")
        try:
            result = self._http(outbound, timeout=request.timeout_seconds)
            if result.status != 200:
                raise ProviderError(f"local provider request failed with HTTP {result.status}")
            payload: Any = json.loads(result.body)
            if not isinstance(payload, dict):
                raise ProviderError("local provider returned malformed response")
            if payload.get("done") is not True:
                raise IncompleteResponseError("local provider response did not complete")
            text = payload.get("response")
            if not isinstance(text, str) or not text.strip():
                raise IncompleteResponseError("local provider returned no text")
            input_tokens = _usage_count(payload.get("prompt_eval_count"))
            output_tokens = _usage_count(payload.get("eval_count"))
            response_id = payload.get("created_at", "")
            if not isinstance(response_id, str):
                raise ProviderError("local provider returned malformed response id")
            reason = payload.get("done_reason")
            if reason != "stop":
                raise IncompleteResponseError("local provider did not finish normally")
            return TextResult(text, response_id, input_tokens, output_tokens, reason)
        except (ProviderError, InvalidRequestError):
            raise
        except (ValueError, UnicodeDecodeError, OSError, TimeoutError):
            raise ProviderError("local provider transport or response failed") from None
