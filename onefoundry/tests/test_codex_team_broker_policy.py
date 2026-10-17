"""Only scoped OneFoundry tools reach a provider from a team turn."""

from __future__ import annotations

import json
from typing import Any

import pytest

from runner.codex_team_broker_policy import (
    TeamRequestDenied,
    TeamRequestLimits,
    admit_request_number,
    admit_team_request,
)

LIMITS = TeamRequestLimits("gpt-6-sol", 131072, 6)
GATEWAY = {
    "type": "namespace",
    "name": "onefoundry",
    "tools": [{"type": "function", "name": "publish_update", "parameters": {"type": "object"}}],
}
NATIVE_PROMPT = {"type": "function", "name": "request_user_input", "parameters": {"type": "object"}}


def _body(**changes: Any) -> bytes:
    payload: dict[str, Any] = {
        "model": "gpt-6-sol",
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": "Work"}],
        "tools": [NATIVE_PROMPT, GATEWAY],
    }
    payload.update(changes)
    return json.dumps(payload).encode()


def test_native_prompt_is_removed_and_request_is_bounded() -> None:
    forwarded = json.loads(admit_team_request(_body(), LIMITS))
    assert forwarded["tools"] == [GATEWAY]
    assert "max_output_tokens" not in forwarded
    assert forwarded["parallel_tool_calls"] is False
    assert forwarded["store"] is False
    admit_request_number(5, LIMITS)
    with pytest.raises(TeamRequestDenied, match="provider_request_count_limit"):
        admit_request_number(6, LIMITS)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"model": "other"}, "provider_model_changed"),
        ({"stream": False}, "provider_request_shape_invalid"),
        ({"max_completion_tokens": 10}, "provider_output_field_unsupported"),
        ({"max_output_tokens": 10}, "provider_output_field_unsupported"),
        ({"tool_choice": "required"}, "provider_tool_choice_invalid"),
        ({"tools": [NATIVE_PROMPT]}, "provider_gateway_tools_missing"),
        ({"tools": [GATEWAY, GATEWAY]}, "provider_tool_not_admitted"),
        ({"tools": [GATEWAY, {"type": "function", "name": "exec_command"}]}, "provider_tool_not_admitted"),
        ({"tools": [GATEWAY, {"type": "web_search"}]}, "provider_tool_not_admitted"),
        (
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "onefoundry",
                        "tools": [{"type": "function", "name": "other"}],
                    }
                ]
            },
            "provider_gateway_tools_invalid",
        ),
    ],
)
def test_unadmitted_request_is_rejected(change: dict[str, Any], reason: str) -> None:
    with pytest.raises(TeamRequestDenied, match=reason):
        admit_team_request(_body(**change), LIMITS)


def test_invalid_json_size_and_limits_are_rejected() -> None:
    with pytest.raises(TeamRequestDenied, match="provider_request_invalid_json"):
        admit_team_request(b"{", LIMITS)
    with pytest.raises(TeamRequestDenied, match="provider_request_size_limit"):
        admit_team_request(b"x" * (LIMITS.max_request_bytes + 1), LIMITS)
    with pytest.raises(ValueError, match="invalid team provider limits"):
        TeamRequestLimits("", 0, 0)
