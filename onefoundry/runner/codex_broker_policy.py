"""Strict request and response bounds for the offline Codex broker proof.

This is a local fixture contract. It does not establish a monetary ceiling or
authorize a connection to a real provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class BrokerDenied(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BrokerLimits:
    max_requests: int = 2
    max_request_bytes: int = 131072
    max_response_bytes: int = 65536
    max_output_tokens_per_request: int = 8
    max_total_tokens_per_request: int = 32
    max_total_tokens: int = 64


LIMITS = BrokerLimits()


def admit_request_number(previous: int, limits: BrokerLimits = LIMITS) -> None:
    if previous < 0 or previous >= limits.max_requests:
        raise BrokerDenied("request_count_limit")


def admit_connect(first_line: bytes) -> None:
    if first_line != b"CONNECT provider.test:443 HTTP/1.1":
        raise BrokerDenied("connect_target_denied")


def admit_request(
    request_line: str, headers: dict[str, str], body: bytes, limits: BrokerLimits = LIMITS
) -> bytes:
    if request_line != "POST /v1/responses HTTP/1.1":
        raise BrokerDenied("request_path_denied")
    if headers.get("host") not in {"provider.test", "provider.test:443"}:
        raise BrokerDenied("request_host_denied")
    if headers.get("authorization") != "Bearer client-placeholder-only":
        raise BrokerDenied("client_credential_invalid")
    if headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise BrokerDenied("request_content_type_invalid")
    if "transfer-encoding" in headers or headers.get("content-length") != str(len(body)):
        raise BrokerDenied("request_framing_invalid")
    if len(body) > limits.max_request_bytes:
        raise BrokerDenied("request_body_limit")
    try:
        payload: Any = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BrokerDenied("request_json_invalid") from exc
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        raise BrokerDenied("request_shape_invalid")
    if payload.get("model") != "gpt-5":
        raise BrokerDenied("request_model_denied")
    if "max_completion_tokens" in payload:
        raise BrokerDenied("alternate_token_limit_denied")
    requested = payload.get("max_output_tokens")
    if requested is not None and (
        type(requested) is not int or not 1 <= requested <= limits.max_output_tokens_per_request
    ):
        raise BrokerDenied("output_token_limit")
    payload["max_output_tokens"] = limits.max_output_tokens_per_request
    bounded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(bounded) > limits.max_request_bytes:
        raise BrokerDenied("request_body_limit")
    return bounded


def admit_total_tokens(previous: int, observed: int, limits: BrokerLimits = LIMITS) -> None:
    if previous < 0 or observed < 0 or previous + observed > limits.max_total_tokens:
        raise BrokerDenied("cumulative_token_limit")


def response_tokens(body: bytes, limits: BrokerLimits = LIMITS) -> int:
    if len(body) > limits.max_response_bytes:
        raise BrokerDenied("response_body_limit")
    completed: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith(b"data: "):
            continue
        try:
            event = json.loads(line[6:])
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrokerDenied("response_stream_invalid") from exc
        if isinstance(event, dict) and event.get("type") == "response.completed":
            completed.append(event)
    if len(completed) != 1:
        raise BrokerDenied("response_usage_missing")
    response = completed[0].get("response")
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise BrokerDenied("response_usage_missing")
    input_count = usage.get("input_tokens")
    output_count = usage.get("output_tokens")
    if type(input_count) is not int or type(output_count) is not int:
        raise BrokerDenied("response_usage_invalid")
    if input_count < 0 or not 0 <= output_count <= limits.max_output_tokens_per_request:
        raise BrokerDenied("response_token_limit")
    total = input_count + output_count
    if total > limits.max_total_tokens_per_request:
        raise BrokerDenied("response_token_limit")
    return total
