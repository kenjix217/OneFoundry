"""Team turns bind each profile's own provider and classify failures without retrying unknowns."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import httpx2 as httpx
import openai
import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_sdk_turn import (
    TaskSessionSdkTurn,
    TeamModel,
    TeamModelUnavailable,
    classify_failure,
    team_model,
)


def _runtime(provider: str, model: str = "m-1", **values: object) -> ProfileTextRuntime:
    defaults: dict[str, object] = {
        "profile_id": uuid4(),
        "instructions": "Work",
        "memory_scope": "project",
        "account_id": uuid4(),
        "provider": provider,
        "model_id": model,
        "credential_revision": 1,
        "endpoint": None,
        "api_key": "test-key",
        "codex_state": None,
        "grok_state": None,
    }
    defaults.update(values)
    return ProfileTextRuntime(**defaults)  # type: ignore[arg-type]


SETTINGS = Settings(db_password="test-only")


@pytest.mark.parametrize("stage", ["bind", "reconcile", "context", "specifications"])
@pytest.mark.parametrize(
    ("failure", "code"),
    [(Conflict, "conflict"), (NotFound, "not_found"), (ValueError, "invalid_value")],
)
def test_preflight_saves_only_owned_stage_and_type(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure: type[Exception],
    code: str,
) -> None:
    import onefoundry.task_session_sdk_turn as sdk_turn

    project_id, attempt_id, session_id, profile_id = (uuid4() for _ in range(4))
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = None
    worker = TaskSessionSdkTurn(cast(Any, SimpleNamespace(engine=engine)), SETTINGS)
    worker.attempts = Mock()
    worker.attempts.settle.return_value = {"outcome": "failed"}
    closed = AsyncMock()
    worker.model_factory = lambda _runtime, _settings: TeamModel(
        cast(Any, object()), "fixture", "fixture", False, False, cast(Any, SimpleNamespace(close=closed))
    )
    monkeypatch.setattr(sdk_turn, "configured_profile", lambda *_args: {})
    monkeypatch.setattr(sdk_turn, "profile_blockers", lambda *_args: [])
    monkeypatch.setattr(sdk_turn, "resolve", lambda *_args: cast(Any, object()))

    def maybe_fail(at: str) -> None:
        if stage == at:
            raise failure("private_key=protected-do-not-save")

    def specifications() -> list[dict[str, object]]:
        maybe_fail("specifications")
        return [{"tools": []}]

    def prepare(*_args: object) -> SimpleNamespace:
        maybe_fail("context")
        return SimpleNamespace(prompt="unused")

    monkeypatch.setattr(sdk_turn, "bind", lambda *_args: maybe_fail("bind"))
    monkeypatch.setattr(
        sdk_turn,
        "TaskSessionTools",
        lambda *_args: SimpleNamespace(
            reconcile_previous_actions=lambda: maybe_fail("reconcile"),
            specifications=specifications,
        ),
    )
    monkeypatch.setattr(TaskSessionContext, "prepare", prepare)
    provider_run = Mock(side_effect=AssertionError("provider must not run"))
    monkeypatch.setattr(sdk_turn, "_run", provider_run)

    assert worker.run_claimed(
        project_id, {"id": attempt_id, "session_id": session_id, "profile_id": profile_id}
    ) == {"outcome": "failed"}
    worker.attempts.settle.assert_called_once_with(
        project_id, attempt_id, "failed", reason=f"runner_preflight_{stage}_{code}"
    )
    assert "protected-do-not-save" not in str(worker.attempts.settle.call_args)
    closed.assert_awaited_once()
    provider_run.assert_not_called()


@pytest.mark.parametrize(
    ("failure_reason", "saved_reason"),
    [
        ("memory_embedding_timeout", "memory_embedding_timeout"),
        ("unexpected protected dependency detail", "runner_preflight_unavailable"),
    ],
)
def test_preflight_memory_failure_settles_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
    saved_reason: str,
) -> None:
    import onefoundry.task_session_sdk_turn as sdk_turn

    project_id, attempt_id, session_id, profile_id = (uuid4() for _ in range(4))
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.scalar.return_value = None
    gateway = cast(Any, SimpleNamespace(engine=engine))
    worker = TaskSessionSdkTurn(gateway, SETTINGS)
    worker.attempts = Mock()
    worker.attempts.settle.return_value = {"outcome": "failed"}
    closed = AsyncMock()
    bound = TeamModel(
        cast(Any, object()), "fixture", "fixture", False, False, cast(Any, SimpleNamespace(close=closed))
    )
    worker.model_factory = lambda _runtime, _settings: bound
    monkeypatch.setattr(sdk_turn, "configured_profile", lambda *_args: {})
    monkeypatch.setattr(sdk_turn, "profile_blockers", lambda *_args: [])
    monkeypatch.setattr(sdk_turn, "resolve", lambda *_args: cast(Any, object()))
    monkeypatch.setattr(sdk_turn, "bind", lambda *_args: None)
    monkeypatch.setattr(
        sdk_turn, "TaskSessionTools", lambda *_args: SimpleNamespace(reconcile_previous_actions=lambda: 0)
    )
    monkeypatch.setattr(
        TaskSessionContext,
        "prepare",
        lambda *_args: (_ for _ in ()).throw(Unavailable(failure_reason)),
    )
    monkeypatch.setattr(sdk_turn, "_run", Mock(side_effect=AssertionError("provider must not run")))

    assert worker.run_claimed(
        project_id, {"id": attempt_id, "session_id": session_id, "profile_id": profile_id}
    ) == {"outcome": "failed"}
    worker.attempts.settle.assert_called_once_with(project_id, attempt_id, "failed", reason=saved_reason)
    closed.assert_awaited_once()


def _base(bound_client: object) -> str:
    assert isinstance(bound_client, openai.AsyncOpenAI)
    return str(bound_client.base_url)


@pytest.mark.parametrize(
    ("provider", "model", "base", "sent_model"),
    [
        ("anthropic", "anthropic/claude-test", "https://api.anthropic.com/v1/", "claude-test"),
        (
            "gemini",
            "models/gemini-test",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "gemini-test",
        ),
        ("grok", "grok-test", "https://api.x.ai/v1/", "grok-test"),
        ("openrouter", "vendor/model", "https://openrouter.ai/api/v1/", "vendor/model"),
    ],
)
def test_chat_compatible_providers_bind_their_official_endpoints(
    provider: str, model: str, base: str, sent_model: str
) -> None:
    bound = team_model(_runtime(provider, model), SETTINGS)
    assert isinstance(bound.model, OpenAIChatCompletionsModel)
    assert (bound.provider, bound.model_id, bound.streamed, bound.responses_api) == (
        provider,
        sent_model,
        False,
        False,
    )
    assert _base(bound.client) == base


def test_openai_codex_and_ollama_routes() -> None:
    openai_bound = team_model(_runtime("openai", "gpt-test"), SETTINGS)
    assert isinstance(openai_bound.model, OpenAIResponsesModel)
    assert (openai_bound.streamed, openai_bound.responses_api) == (False, True)
    codex = team_model(
        _runtime(
            "openai_codex_runtime",
            "codex_account/gpt-test",
            api_key=None,
            codex_state={"access_token": "token-value", "account_id": "acct-1"},
        ),
        SETTINGS,
    )
    assert isinstance(codex.model, OpenAIResponsesModel)
    assert (codex.model_id, codex.streamed, codex.responses_api) == ("gpt-test", True, True)
    assert _base(codex.client) == "https://chatgpt.com/backend-api/codex/"
    assert codex.client is not None and codex.client.default_headers["chatgpt-account-id"] == "acct-1"
    local = team_model(
        _runtime("ollama", "qwen:latest", api_key=None, endpoint="http://ollama:11434"), SETTINGS
    )
    assert _base(local.client) == "http://ollama:11434/v1/"
    with pytest.raises(TeamModelUnavailable):
        team_model(_runtime("ollama", api_key=None, endpoint="http://example.com:11434"), SETTINGS)
    with pytest.raises(TeamModelUnavailable):
        team_model(_runtime("anthropic", api_key=None), SETTINGS)


@pytest.mark.parametrize("catalog_model", ["grok-test", "grok_account/grok-test"])
def test_grok_account_binds_streamed_cli_proxy_with_account_headers(catalog_model: str) -> None:
    bound = team_model(
        _runtime(
            "xai",
            catalog_model,
            api_key=None,
            grok_state={"kind": "connected", "access_token": "test-token"},
        ),
        SETTINGS,
    )
    assert isinstance(bound.model, OpenAIChatCompletionsModel)
    assert (bound.provider, bound.model_id, bound.streamed, bound.responses_api) == (
        "xai",
        "grok-test",
        True,
        False,
    )
    assert _base(bound.client) == "https://cli-chat-proxy.grok.com/v1/"
    assert bound.client is not None
    assert bound.client.default_headers["X-XAI-Token-Auth"] == "xai-grok-cli"
    assert bound.client.default_headers["x-grok-model-override"] == "grok-test"


@pytest.mark.parametrize(
    "model,state",
    [
        ("other/model", {"kind": "connected", "access_token": "test-token"}),
        ("grok_account/bad\r\nheader", {"kind": "connected", "access_token": "test-token"}),
        ("grok_account/grok-test", {"kind": "connected", "access_token": "bad\r\nheader"}),
        ("grok-test", {"kind": "connected", "access_token": "bad\x00header"}),
        ("grok-test", {"kind": "connected", "access_token": ""}),
        ("grok-test", {"kind": "connected", "access_token": 42}),
        ("grok_account/grok-test", {"kind": "pending", "access_token": "test-token"}),
        ("grok_account/grok-test", None),
    ],
)
def test_grok_account_rejects_invalid_binding(model: str, state: dict[str, object] | None) -> None:
    with pytest.raises(TeamModelUnavailable):
        team_model(_runtime("xai", model, api_key="wrong-api-key", grok_state=state), SETTINGS)


def test_grok_account_sdk_wire_and_tool_loop_without_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import json
    from unittest.mock import Mock

    import onefoundry.task_session_sdk_turn as sdk_turn
    from onefoundry.task_session_tools import TaskSessionTools

    requests: list[httpx.Request] = []

    def event(delta: dict[str, object], finish: str | None = None) -> str:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "grok-test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            content = event(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "publish_update",
                                "arguments": '{"body":"Checkpoint"}',
                            },
                        }
                    ],
                }
            ) + event({}, "tool_calls")
        else:
            content = event({"role": "assistant", "content": "Checkpoint saved."}) + event({}, "stop")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=content + "data: [DONE]\n\n",
        )

    def client(
        settings: Settings, base_url: str, api_key: str, headers: dict[str, str] | None = None
    ) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(sdk_turn, "_client", client)
    bound = team_model(
        _runtime(
            "xai",
            "grok_account/grok-test",
            api_key=None,
            grok_state={"kind": "connected", "access_token": "test-token"},
        ),
        SETTINGS,
    )
    saved = Mock(spec=TaskSessionTools)
    saved.invoke.return_value = {
        "success": True,
        "contentItems": [{"type": "inputText", "text": '{"saved":true}'}],
    }
    spec = {
        "name": "publish_update",
        "description": "Save checkpoint",
        "inputSchema": {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
            "additionalProperties": False,
        },
    }
    outcome = asyncio.run(sdk_turn._run(bound, "Prepare", [spec], saved, None, SETTINGS))
    assert (outcome.status, outcome.reply) == ("done", "Checkpoint saved.")
    assert len(requests) == 2
    for request in requests:
        assert request.url == "https://cli-chat-proxy.grok.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["x-xai-token-auth"] == "xai-grok-cli"
        assert request.headers["x-grok-model-override"] == "grok-test"
        body = json.loads(request.content)
        assert body["model"] == "grok-test"
        assert body["stream"] is True
        assert body["store"] is False
        assert body["max_completion_tokens"] == SETTINGS.team_max_output_tokens
        assert "max_tokens" not in body
        assert body["tools"][0]["function"]["name"] == "publish_update"
    second = json.loads(requests[1].content)
    assert any(message.get("role") == "tool" for message in second["messages"])
    saved.invoke.assert_called_once()


def _status(code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/responses")
    return openai.APIStatusError("provider error", response=httpx.Response(code, request=request), body=None)


@pytest.mark.parametrize("code", ["server_is_overloaded", "unknown_provider_error", None])
def test_streamed_provider_rejection_uses_structured_code_only(code: str | None) -> None:
    error = openai.APIError(
        "server_is_overloaded private provider message must not be retained",
        request=httpx.Request("POST", "https://provider.test/v1/responses"),
        body={"code": code},
    )
    wrapped = RuntimeError("SDK wrapper")
    wrapped.__cause__ = error
    result = classify_failure(wrapped)
    if code == "server_is_overloaded":
        assert (result.status, result.reason, result.retryable) == ("failed", "provider_unavailable", True)
    else:
        assert (result.status, result.reason, result.retryable) == (
            "unknown",
            "provider_outcome_unknown",
            False,
        )


def test_failure_classification_retries_only_provably_unprocessed_requests() -> None:
    request = httpx.Request("POST", "https://provider.test/v1/responses")
    cases = {
        "rate_limited": (_status(429), ("failed", "provider_rate_limited", True)),
        "overloaded": (_status(503), ("failed", "provider_unavailable", True)),
        "auth": (_status(401), ("failed", "provider_auth_failed", False)),
        "bad_request": (_status(400), ("failed", "provider_rejected_request", False)),
        "server": (_status(500), ("failed", "provider_error", False)),
        "timeout": (openai.APITimeoutError(request), ("unknown", "provider_outcome_unknown", False)),
        "max_turns": (MaxTurnsExceeded("limit"), ("failed", "turn_limit_reached", False)),
        "tool": (RuntimeError("tool"), ("unknown", "provider_outcome_unknown", False)),
        "unadmitted_model_action": (
            ModelBehaviorError("Tool is not in the admitted SDK catalogue"),
            ("failed", "invalid_model_action", False),
        ),
    }
    for name, (error, expected) in cases.items():
        outcome = classify_failure(error)
        assert (outcome.status, outcome.reason, outcome.retryable) == expected, name
    try:
        raise httpx.ConnectError("refused")
    except httpx.ConnectError as cause:
        refused = openai.APIConnectionError(request=request)
        refused.__cause__ = cause
    assert classify_failure(refused).retryable is True
    try:
        raise httpx.ReadError("reset")
    except httpx.ReadError as cause:
        dropped = openai.APIConnectionError(request=request)
        dropped.__cause__ = cause
    assert (classify_failure(dropped).status, classify_failure(dropped).retryable) == ("unknown", False)
    wrapped = RuntimeError("sdk wrapper")
    wrapped.__cause__ = Conflict("team tool call outcome changed")
    assert classify_failure(wrapped).reason == "team_tool_outcome_unknown"


def test_turn_timeout_must_be_shorter_than_abandoned_turn_bound() -> None:
    with pytest.raises(ValueError, match="team turn timeout"):
        Settings(db_password="test-only", team_turn_timeout_seconds=600, onefoundry_max_elapsed_seconds=600)


@pytest.mark.parametrize("streamed", [False, True])
def test_default_sdk_attempt_can_finish_after_more_than_eight_tool_calls(streamed: bool) -> None:
    import asyncio
    import json
    from unittest.mock import Mock

    from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

    from onefoundry.task_session_sdk_turn import TeamModel, _run
    from onefoundry.task_session_tools import TaskSessionTools

    saved = Mock(spec=TaskSessionTools)
    saved.invoke.side_effect = lambda name, arguments, call_id: {
        "success": True,
        "contentItems": [{"type": "inputText", "text": json.dumps({"message_id": str(uuid4())})}],
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[function_call("publish_update", {"body": f"Checkpoint {i}"}, call_id=f"call-{i}")]
            )
            for i in range(10)
        ]
        + [ModelStep(output=[assistant_message("Preparation complete; recording review is pending.")])]
    )
    spec = {
        "name": "publish_update",
        "description": "Save checkpoint",
        "inputSchema": {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
            "additionalProperties": False,
        },
    }
    result = asyncio.run(
        _run(
            TeamModel(model, "fixture", "fixture", streamed, False), "Prepare", [spec], saved, None, SETTINGS
        )
    )
    assert (result.status, result.reason) == ("done", None)
    assert saved.invoke.call_count == 10
    assert len({call.args[2] for call in saved.invoke.call_args_list}) == 10


@pytest.mark.parametrize("streamed", [False, True])
def test_successful_report_result_stops_before_another_model_call(streamed: bool) -> None:
    import asyncio
    import json

    from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

    from onefoundry.task_session_sdk_turn import _run
    from onefoundry.task_session_tools import TaskSessionTools

    saved = Mock(spec=TaskSessionTools)
    saved.invoke.return_value = {
        "success": True,
        "contentItems": [{"type": "inputText", "text": json.dumps({"status": "saved"})}],
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[function_call("report_result", {"body": "Verified finding."}, call_id="report-ok")]
            ),
            ModelStep(output=[assistant_message("A stale extra model call happened.")]),
        ]
    )
    spec = {
        "name": "report_result",
        "description": "Save a result",
        "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
    }
    outcome = asyncio.run(
        _run(
            TeamModel(model, "fixture", "fixture", streamed, False),
            "Report",
            [spec],
            saved,
            None,
            SETTINGS.model_copy(update={"team_turn_timeout_seconds": 2}),
        )
    )
    assert (outcome.status, outcome.reply) == ("done", None)
    assert model.remaining_steps == 1
    saved.invoke.assert_called_once()


def test_failed_report_result_stays_nonterminal_and_can_receive_correction() -> None:
    import asyncio
    import json

    from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

    from onefoundry.task_session_sdk_turn import _run
    from onefoundry.task_session_tools import TaskSessionTools

    saved = Mock(spec=TaskSessionTools)
    saved.invoke.return_value = {
        "success": False,
        "contentItems": [{"type": "inputText", "text": json.dumps({"reason": "invalid_tool_input"})}],
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[function_call("report_result", {"body": "Invalid finding."}, call_id="report-bad")]
            ),
            ModelStep(output=[assistant_message("Report rejected; a corrected source is needed.")]),
        ]
    )
    spec = {
        "name": "report_result",
        "description": "Save a result",
        "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
    }
    outcome = asyncio.run(
        _run(
            TeamModel(model, "fixture", "fixture", False, False),
            "Report",
            [spec],
            saved,
            None,
            SETTINGS.model_copy(update={"team_turn_timeout_seconds": 2}),
        )
    )
    assert (outcome.status, outcome.reply) == ("done", "Report rejected; a corrected source is needed.")
    assert model.remaining_steps == 0
    saved.invoke.assert_called_once()


def test_successful_submit_completion_keeps_submitted_public_reply() -> None:
    import asyncio
    import json

    from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

    from onefoundry.task_session_sdk_turn import _run
    from onefoundry.task_session_tools import TaskSessionTools

    saved = Mock(spec=TaskSessionTools)
    saved.invoke.return_value = {
        "success": True,
        "contentItems": [{"type": "inputText", "text": json.dumps({"status": "needs_review"})}],
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call("submit_completion", {"body": "Finished after QA."}, call_id="complete-ok")
                ]
            ),
            ModelStep(output=[assistant_message("A stale extra model call happened.")]),
        ]
    )
    spec = {
        "name": "submit_completion",
        "description": "Submit the completion",
        "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
    }
    outcome = asyncio.run(
        _run(
            TeamModel(model, "fixture", "fixture", False, False),
            "Complete",
            [spec],
            saved,
            None,
            SETTINGS.model_copy(update={"team_turn_timeout_seconds": 2}),
        )
    )
    assert (outcome.status, outcome.reply) == ("done", None)
    assert model.remaining_steps == 1
    saved.invoke.assert_called_once()


def test_second_tool_in_terminal_model_response_does_not_enter_closed_session() -> None:
    import asyncio
    import json

    from agents.testing import ModelStep, ScriptedModel, function_call

    from onefoundry.task_session_sdk_turn import _run
    from onefoundry.task_session_tools import TaskSessionTools

    saved = Mock(spec=TaskSessionTools)
    saved.invoke.return_value = {
        "success": True,
        "contentItems": [{"type": "inputText", "text": json.dumps({"status": "saved"})}],
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call("report_result", {"body": "Verified finding."}, call_id="report-ok"),
                    function_call("publish_update", {"body": "Stale update."}, call_id="update-stale"),
                ]
            )
        ]
    )
    specs = [
        {
            "name": name,
            "description": f"Use {name}",
            "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
        }
        for name in ("report_result", "publish_update")
    ]
    outcome = asyncio.run(
        _run(
            TeamModel(model, "fixture", "fixture", False, False),
            "Report",
            specs,
            saved,
            None,
            SETTINGS.model_copy(update={"team_turn_timeout_seconds": 2}),
        )
    )
    assert (outcome.status, outcome.reply) == ("done", None)
    saved.invoke.assert_called_once()
