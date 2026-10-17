"""Allowlisted broker counters and failure codes, without upstream content."""

from __future__ import annotations

import json
import re
from typing import Any

from runner.repository_provider_broker import REJECTION_CODES

DENIALS = frozenset(
    {
        *("repository_provider_rejected_" + code for code in REJECTION_CODES | {"unknown"}),
        "repository_provider_incomplete",
        "repository_provider_terminal_missing",
        "repository_provider_origin_invalid",
        "repository_fixture_route_invalid",
        "repository_provider_address_denied",
        "repository_subscription_route_invalid",
        "repository_provider_response_invalid",
        "repository_provider_response_denied",
        "repository_provider_terminal_unproved",
        "repository_transport_timeout",
        "repository_transport_dns_failed",
        "repository_transport_tls_failed",
        "repository_transport_incomplete_body",
        "repository_transport_connection_closed",
        "repository_transport_protocol_failed",
        "repository_broker_client_denied",
        "repository_broker_framing_invalid",
        "repository_broker_body_limit",
        "repository_broker_body_incomplete",
        "repository_provider_bounds_invalid",
        "repository_provider_tools_invalid",
        "repository_provider_tool_invalid",
        "repository_provider_tool_denied",
        "repository_provider_request_limit",
        "repository_provider_request_identity_invalid",
        "repository_provider_tool_choice_denied",
        "repository_subscription_output_field_unsupported",
        "provider_transport_unavailable",
    }
)


def valid_denial(reason: Any) -> bool:
    return isinstance(reason, str) and (
        reason in DENIALS or re.fullmatch(r"repository_provider_http_[1-5][0-9]{2}", reason) is not None
    )


def summarize(raw: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": "repository.broker",
        "forwarded_requests": 0,
        "request_bytes": 0,
        "response_bytes": 0,
        "last_denial": None,
    }
    for line in raw[:65536].splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if (
            value.get("type") == "repository_broker.retrying"
            and set(value) == {"type", "request_number"}
            and type(value["request_number"]) is int
            and 1 <= value["request_number"] <= 100
        ):
            summary["retried_requests"] = summary.get("retried_requests", 0) + 1
        if value.get("type") == "repository_broker.denied" and set(value) == {"type", "reason"}:
            reason = value["reason"]
            if valid_denial(reason):
                summary["last_denial"] = reason
        if value.get("type") != "repository_broker.forwarded" or set(value) != {
            "type",
            "request_number",
            "request_bytes",
            "response_bytes",
        }:
            continue
        if not all(
            type(value[key]) is int and 0 <= value[key] <= bound
            for key, bound in (
                ("request_number", 100),
                ("request_bytes", 4_000_000),
                ("response_bytes", 16_000_000),
            )
        ):
            continue
        summary["forwarded_requests"] = max(summary["forwarded_requests"], value["request_number"])
        summary["request_bytes"] += value["request_bytes"]
        summary["response_bytes"] += value["response_bytes"]
    return summary
