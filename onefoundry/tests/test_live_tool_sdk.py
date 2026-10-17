"""OpenAI API-key role tools use the installed SDK's bounded continuation."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage
from sqlalchemy import create_engine

from onefoundry.role_tool_admission import ToolDenied


def _module() -> Any:
    return importlib.import_module("onefoundry.live_tool_sdk")


@dataclass
class FakeGateway:
    engine: Any = field(default_factory=lambda: create_engine("sqlite+pysqlite:///:memory:"))
    invocations: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    deny: bool = False

    def available_role_tools(self, project_id: UUID, run_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
        return [
            {
                "id": "current_work_order",
                "version": 1,
                "description": "Read this work order",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ]

    def invoke_role_tool(
        self,
        project_id: UUID,
        run_id: UUID,
        profile_id: UUID,
        call_id: str,
        tool_id: str,
        arguments: object,
        *,
        tool_version: int,
    ) -> dict[str, Any]:
        self.invocations.append(
            {
                "project_id": project_id,
                "run_id": run_id,
                "profile_id": profile_id,
                "call_id": call_id,
                "tool_id": tool_id,
                "arguments": arguments,
                "version": tool_version,
            }
        )
        if self.deny:
            raise ToolDenied("tool permission revoked")
        return {"result": {"status": "running", "title": "Secret project prompt"}}

    def _event(
        self,
        connection: object,
        project_id: UUID,
        work_order_id: UUID,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> None:
        self.events.append((event_type, payload))


def _prepared() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai",
        api_key="unit-test-key",
        run_id=uuid4(),
        order_id=uuid4(),
        profile_id=uuid4(),
        model_id="gpt-test",
        instructions="Read only the admitted tool.",
        prompt="Secret project prompt",
        reasoning_effort=None,
    )


def _settings(*, max_turns: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        live_text_openai_base_url="https://api.openai.com/v1",
        live_text_max_output_tokens=100,
        live_text_timeout_seconds=10,
        live_text_tool_max_turns=max_turns,
    )


def test_sdk_invokes_run_bound_gateway_and_continues_to_final_text() -> None:
    module = _module()
    gateway, prepared, project_id = FakeGateway(), _prepared(), uuid4()
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[function_call("current_work_order", {}, call_id="provider-call-7")],
                usage=Usage(input_tokens=13, output_tokens=2, total_tokens=15),
                response_id="resp-1",
            ),
            ModelStep(
                output=[assistant_message("The work order is running.")],
                usage=Usage(input_tokens=19, output_tokens=6, total_tokens=25),
                response_id="resp-2",
            ),
        ]
    )
    result = module.run_live_tool_sdk(gateway, project_id, prepared, _settings(), model=scripted)
    assert result.text == "The work order is running."
    assert result.provider_response_id == "resp-2"
    assert (result.input_tokens, result.output_tokens) == (32, 8)
    assert gateway.invocations == [
        {
            "project_id": project_id,
            "run_id": prepared.run_id,
            "profile_id": prepared.profile_id,
            "call_id": "provider-call-7",
            "tool_id": "current_work_order",
            "arguments": {},
            "version": 1,
        }
    ]
    assert [name for name, _ in gateway.events] == [
        "live_model_call_started",
        "live_model_call_finished",
        "live_model_call_started",
        "live_model_call_finished",
    ]
    assert "Secret project prompt" not in json.dumps(gateway.events)
    assert scripted.remaining_steps == 0


def test_gateway_denial_is_fatal_and_does_not_continue_model() -> None:
    module = _module()
    gateway, prepared, project_id = FakeGateway(deny=True), _prepared(), uuid4()
    scripted = ScriptedModel(
        [
            ModelStep(output=[function_call("current_work_order", {}, call_id="denied-call")]),
            ModelStep(output=[assistant_message("incorrect continuation")]),
        ]
    )
    with pytest.raises(ToolDenied):
        module.run_live_tool_sdk(gateway, project_id, prepared, _settings(), model=scripted)
    assert len(gateway.invocations) == 1
    assert scripted.remaining_steps == 1


def test_max_turns_stops_additional_provider_call() -> None:
    module = _module()
    gateway, prepared, project_id = FakeGateway(), _prepared(), uuid4()
    scripted = ScriptedModel(
        [
            ModelStep(output=[function_call("current_work_order", {}, call_id="first")]),
            ModelStep(output=[assistant_message("unreachable")]),
        ]
    )
    with pytest.raises(Exception, match="turn"):
        module.run_live_tool_sdk(gateway, project_id, prepared, _settings(max_turns=1), model=scripted)
    assert scripted.remaining_steps == 1


def test_only_openai_api_key_transport_can_enter_live_tool_route() -> None:
    module = _module()
    gateway, prepared = FakeGateway(), _prepared()
    prepared.provider = "openai_codex_runtime"
    with pytest.raises(ValueError, match="OpenAI API"):
        module.run_live_tool_sdk(gateway, uuid4(), prepared, _settings(), model=ScriptedModel([]))
    assert gateway.invocations == [] and gateway.events == []


@pytest.mark.parametrize("tool_id", ["github_pr_read_v1", "repository_file_v1", "coding_artifact_text_v1"])
def test_raw_and_pr_reads_are_not_advertised_without_release_gate(tool_id: str) -> None:
    module = _module()
    gateway, prepared = FakeGateway(), _prepared()
    with patch.object(
        gateway,
        "available_role_tools",
        return_value=[
            {
                "id": tool_id,
                "version": 1,
                "description": "Approved PR",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    ):
        with pytest.raises(ToolDenied, match="no admitted tools"):
            module.run_live_tool_sdk(gateway, uuid4(), prepared, _settings(), model=ScriptedModel([]))
    assert gateway.invocations == []
