"""Exact model and local coding-tool admission for the provider broker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

LOCAL_TOOLS = frozenset(
    {
        "exec_command",
        "write_stdin",
        "shell",
        "shell_command",
        "apply_patch",
        "update_plan",
        "view_image",
        "request_user_input",
        "spawn_agent",
        "send_input",
        "wait",
        "wait_agent",
        "close_agent",
        "resume_agent",
        "create_goal",
        "get_goal",
        "update_goal",
    }
)


@dataclass(frozen=True)
class ProviderBounds:
    model_id: str
    max_requests: int
    max_request_bytes: int
    max_response_bytes: int
    max_output_tokens: int
    auth_kind: Literal["api_key", "codex_subscription"] = "api_key"

    def __post_init__(self) -> None:
        if (
            not self.model_id
            or not 1 <= len(self.model_id) <= 120
            or not 1 <= self.max_requests <= 100
            or not 1024 <= self.max_request_bytes <= 4_000_000
            or not 1024 <= self.max_response_bytes <= 16_000_000
            or not 1 <= self.max_output_tokens <= 64000
        ):
            raise ValueError("repository_provider_bounds_invalid")


def _tools(values: Any, depth: int = 0) -> None:
    if depth > 2 or not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("repository_provider_tools_invalid")
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("repository_provider_tool_invalid")
        if item.get("type") == "namespace" and item.get("name") in {"functions", "multi_agent_v1"}:
            _tools(item.get("tools"), depth + 1)
        elif item.get("type") not in {"function", "custom"} or item.get("name") not in LOCAL_TOOLS:
            raise ValueError("repository_provider_tool_denied")


def admit_request(raw: bytes, bounds: ProviderBounds, previous: int) -> bytes:
    if not 0 <= previous < bounds.max_requests or len(raw) > bounds.max_request_bytes:
        raise ValueError("repository_provider_request_limit")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("model") != bounds.model_id
        or value.get("stream") is not True
    ):
        raise ValueError("repository_provider_request_identity_invalid")
    _tools(value.get("tools"))
    if value.get("tool_choice", "auto") not in ("auto", None):
        raise ValueError("repository_provider_tool_choice_denied")
    value["store"] = False
    if bounds.auth_kind == "api_key":
        value["max_output_tokens"] = bounds.max_output_tokens
    elif "max_output_tokens" in value or "max_completion_tokens" in value:
        raise ValueError("repository_subscription_output_field_unsupported")
    result = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    if len(result) > bounds.max_request_bytes:
        raise ValueError("repository_provider_request_limit")
    return result
