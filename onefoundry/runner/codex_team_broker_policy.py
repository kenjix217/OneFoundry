"""Provider-request admission for a Codex team turn behind a credential broker.

Codex can advertise native tools even when the OneFoundry bridge is configured.
The broker forwards only the scoped OneFoundry namespace. This policy neither
holds a credential nor sends a request; the isolated broker owns those steps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

ALLOWED_ACTIONS = frozenset({"publish_update", "delegate_agent", "report_result"})


class TeamRequestDenied(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TeamRequestLimits:
    model_id: str
    max_request_bytes: int
    max_requests: int

    def __post_init__(self) -> None:
        if (
            not self.model_id
            or any(char.isspace() for char in self.model_id)
            or not 1 <= self.max_request_bytes <= 1_000_000
            or not 1 <= self.max_requests <= 100
        ):
            raise ValueError("invalid team provider limits")


def admit_request_number(previous: int, limits: TeamRequestLimits) -> None:
    if previous < 0 or previous >= limits.max_requests:
        raise TeamRequestDenied("provider_request_count_limit")


def admit_team_request(raw: bytes, limits: TeamRequestLimits) -> bytes:
    """Return one bounded request with only the admitted Gateway namespace."""
    if len(raw) > limits.max_request_bytes:
        raise TeamRequestDenied("provider_request_size_limit")
    try:
        payload: Any = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise TeamRequestDenied("provider_request_invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        raise TeamRequestDenied("provider_request_shape_invalid")
    if payload.get("model") != limits.model_id:
        raise TeamRequestDenied("provider_model_changed")
    # ChatGPT-plan Responses rejects these fields; a local response-byte bound
    # is not a provider-side output-token budget.
    if "max_completion_tokens" in payload or "max_output_tokens" in payload:
        raise TeamRequestDenied("provider_output_field_unsupported")
    if payload.get("tool_choice", "auto") not in ("auto", None):
        raise TeamRequestDenied("provider_tool_choice_invalid")
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        raise TeamRequestDenied("provider_tools_missing")
    namespace: dict[str, Any] | None = None
    for tool in tools:
        if not isinstance(tool, dict):
            raise TeamRequestDenied("provider_tool_invalid")
        if tool.get("type") == "function" and tool.get("name") == "request_user_input":
            continue  # Codex's native prompt tool is not a OneFoundry action.
        if tool.get("type") != "namespace" or tool.get("name") != "onefoundry" or namespace is not None:
            raise TeamRequestDenied("provider_tool_not_admitted")
        children = tool.get("tools")
        if (
            not isinstance(children, list)
            or not children
            or len(children) > len(ALLOWED_ACTIONS)
            or any(
                not isinstance(child, dict)
                or child.get("type") != "function"
                or child.get("name") not in ALLOWED_ACTIONS
                for child in children
            )
        ):
            raise TeamRequestDenied("provider_gateway_tools_invalid")
        names = [child["name"] for child in children]
        if len(names) != len(set(names)):
            raise TeamRequestDenied("provider_gateway_tools_duplicate")
        namespace = tool
    if namespace is None:
        raise TeamRequestDenied("provider_gateway_tools_missing")
    payload["tools"] = [namespace]
    payload["parallel_tool_calls"] = False
    payload["store"] = False
    bounded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(bounded) > limits.max_request_bytes:
        raise TeamRequestDenied("provider_request_size_limit")
    return bounded
