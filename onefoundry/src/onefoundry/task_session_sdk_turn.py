"""Run one claimed team turn on the agent's own configured text provider.

The installed OpenAI Agents SDK owns the model/tool loop for every provider;
OneFoundry adds no loop of its own. The scoped team tools remain the only
authority for an action. Each turn resolves the session profile's current model
account, so an agent can switch provider or model between turns while its
saved session, receipts, and history stay in OneFoundry.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx2
from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
    Runner,
    ToolsToFinalOutputResult,
    set_tracing_disabled,
)
from agents import _debug as agents_debug
from agents.models.interface import Model
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import FunctionToolResult, set_function_tool_failure_error_function
from agents.tool_context import ToolContext
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)
from openai.types.shared import Reasoning, ReasoningEffort
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.grok_account import valid_account_token, valid_model_id
from onefoundry.inference import InvalidRequestError
from onefoundry.inference.ollama import _local_endpoint
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.profile_text_runtime import ProfileTextRuntime, resolve
from onefoundry.provider_registry import provider_spec
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import ToolDependencyFailure, ToolOutcomeUnknown
from onefoundry.task_session_admission import configured_profile, profile_blockers
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_dependency_context import TeamContextBudgetExceeded
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

CODEX_ACCOUNT_BASE_URL = "https://chatgpt.com/backend-api/codex"
MEMORY_UNAVAILABLE_REASONS = frozenset(
    {
        "memory_embedding_asset_invalid",
        "memory_embedding_busy",
        "memory_embedding_content_unavailable",
        "memory_embedding_dimensions_mismatch",
        "memory_embedding_index_incomplete",
        "memory_embedding_model_mismatch",
        "memory_embedding_response_invalid",
        "memory_embedding_timeout",
        "memory_embedding_unavailable",
    }
)
TEAM_INSTRUCTIONS = (
    "You are one agent on a OneFoundry work-order team. Work from the saved task data in the "
    "user message. Use the provided tools for every saved action; describing an action in prose "
    "does not perform it. Finish with a brief public reply: result or blocker, and next step."
)


class TeamModelUnavailable(Exception):
    """The profile's provider cannot run team tools on this route."""


@dataclass(frozen=True)
class TeamModel:
    model: Model
    provider: str
    model_id: str
    streamed: bool
    responses_api: bool
    client: AsyncOpenAI | None = None


ModelFactory = Callable[[ProfileTextRuntime, "Settings"], TeamModel]


def _client(
    settings: Settings, base_url: str, api_key: str, headers: dict[str, str] | None = None
) -> AsyncOpenAI:
    timeout = settings.team_turn_timeout_seconds
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        timeout=timeout,
        default_headers=headers,
        http_client=DefaultAsyncHttpxClient(trust_env=False, follow_redirects=False, timeout=timeout),
    )


def _strip(model_id: str, *prefixes: str) -> str:
    for prefix in prefixes:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def team_model(runtime: ProfileTextRuntime, settings: Settings) -> TeamModel:
    """Bind the profile's current account to an SDK model; nothing is sent here."""
    provider = runtime.provider
    if provider == "openai_codex_runtime" and runtime.codex_state is not None:
        # The ChatGPT-plan Responses route accepts only streamed requests and no output cap.
        model_id = _strip(runtime.model_id, "codex_account/")
        client = _client(
            settings,
            CODEX_ACCOUNT_BASE_URL,
            str(runtime.codex_state["access_token"]),
            {
                "chatgpt-account-id": str(runtime.codex_state["account_id"]),
                "openai-beta": "responses=experimental",
                "originator": "onefoundry",
            },
        )
        return TeamModel(OpenAIResponsesModel(model_id, client), provider, model_id, True, True, client)
    if provider == "openai" and runtime.api_key:
        client = _client(settings, settings.live_text_openai_base_url, runtime.api_key)
        model_id = runtime.model_id
        return TeamModel(OpenAIResponsesModel(model_id, client), provider, model_id, False, True, client)
    if provider == "xai":
        state = runtime.grok_state
        model_id = runtime.model_id.removeprefix("grok_account/")
        token = state.get("access_token") if state is not None else None
        if (
            state is None
            or state.get("kind") != "connected"
            or not valid_model_id(model_id)
            or not isinstance(token, str)
            or not valid_account_token(token)
        ):
            raise TeamModelUnavailable("Grok model account is unavailable")
        client = _client(
            settings,
            settings.grok_account_base_url,
            token,
            {
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-model-override": model_id,
            },
        )
        return TeamModel(
            OpenAIChatCompletionsModel(model_id, client), provider, model_id, True, False, client
        )
    base_url: str | None = None
    api_key = runtime.api_key
    model_id = runtime.model_id
    if provider == "anthropic":
        base_url, model_id = settings.live_text_anthropic_base_url, _strip(model_id, "anthropic/")
    elif provider == "gemini":
        base_url = settings.live_text_gemini_base_url + "/openai"
        model_id = _strip(model_id, "google/", "gemini/", "models/")
    elif provider == "grok":
        base_url = settings.live_text_grok_base_url
    elif provider == "ollama" and runtime.endpoint is not None:
        try:
            base_url = _local_endpoint(runtime.endpoint).removesuffix("/api/generate") + "/v1"
        except InvalidRequestError:
            raise TeamModelUnavailable("local model endpoint is invalid") from None
        api_key = "ollama"
    elif (spec := provider_spec(provider)) is not None and spec.family == "openai_compatible":
        base_url = spec.base_url
    if base_url is None or not api_key:
        raise TeamModelUnavailable("this model account cannot run team tools yet")
    client = _client(settings, base_url, api_key)
    return TeamModel(OpenAIChatCompletionsModel(model_id, client), provider, model_id, False, False, client)


@dataclass(frozen=True)
class TurnOutcome:
    status: str
    reason: str | None = None
    reply: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    retryable: bool = False


@dataclass
class _TerminalTurnState:
    saved: bool = False


def _cause(error: BaseException, kinds: tuple[type[BaseException], ...]) -> BaseException | None:
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            return None
        if isinstance(current, kinds):
            return current
        current = current.__cause__ or current.__context__
    return None


def classify_failure(error: BaseException) -> TurnOutcome:
    """Map an SDK/provider failure to a saved outcome without provider text."""
    if _cause(error, (ToolOutcomeUnknown, Conflict, NotFound)) is not None:
        return TurnOutcome("unknown", "team_tool_outcome_unknown")
    if _cause(error, (ToolDependencyFailure,)) is not None:
        return TurnOutcome("failed", "tool_dependency_failure")
    if _cause(error, (ModelBehaviorError,)) is not None:
        return TurnOutcome("failed", "invalid_model_action")
    if isinstance(error, MaxTurnsExceeded):
        return TurnOutcome("failed", "turn_limit_reached")
    status = _cause(error, (APIStatusError,))
    if isinstance(status, APIStatusError):
        code = status.status_code
        if code == 429:
            return TurnOutcome("failed", "provider_rate_limited", retryable=True)
        if code == 503:
            return TurnOutcome("failed", "provider_unavailable", retryable=True)
        if code in (401, 403):
            return TurnOutcome("failed", "provider_auth_failed")
        if 400 <= code < 500:
            return TurnOutcome("failed", "provider_rejected_request")
        return TurnOutcome("failed", "provider_error")
    rejection = _cause(error, (APIError,))
    if isinstance(rejection, APIError) and rejection.code == "server_is_overloaded":
        # Streamed subscription rejections have no HTTP failure status. The
        # attempt owner still forbids automatic retry after any tool receipt.
        return TurnOutcome("failed", "provider_unavailable", retryable=True)
    if _cause(error, (APITimeoutError,)) is not None:
        return TurnOutcome("unknown", "provider_outcome_unknown")
    connection = _cause(error, (APIConnectionError,))
    if connection is not None and _cause(connection, (httpx2.ConnectError,)) is not None:
        # No connection was established, so the provider never saw the request.
        return TurnOutcome("failed", "provider_unavailable", retryable=True)
    return TurnOutcome("unknown", "provider_outcome_unknown")


def _function_tools(
    specifications: list[dict[str, Any]], tools: TaskSessionTools, terminal: _TerminalTurnState
) -> list[FunctionTool]:
    lock = asyncio.Lock()
    calls = 0
    output: list[FunctionTool] = []
    for spec in specifications:
        name = str(spec["name"])

        async def invoke(context: ToolContext[Any], raw: str, *, _name: str = name) -> str:
            nonlocal calls
            async with lock:
                if terminal.saved:
                    return json.dumps({"success": False, "reason": "terminal_result_already_saved"})
                calls += 1
                try:
                    arguments = json.loads(raw) if raw else {}
                except ValueError:
                    arguments = None
                if not isinstance(arguments, dict):
                    return json.dumps({"success": False, "reason": "invalid_tool_input"})
                # Prefix with a per-turn counter: some providers reuse simple call IDs.
                call_id = f"{calls}-{context.tool_call_id}"[:200]
                response = await asyncio.to_thread(tools.invoke, f"onefoundry.{_name}", arguments, call_id)
                content = json.loads(response["contentItems"][0]["text"])
                result = {"success": response["success"], **content}
                if response["success"] is True and _name == "report_result":
                    terminal.saved = True
                elif response["success"] is True and _name == "submit_completion":
                    terminal.saved = True
                return json.dumps(result, sort_keys=True)

        tool = FunctionTool(
            name=name,
            description=str(spec["description"]),
            params_json_schema=dict(spec["inputSchema"]),
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )
        set_function_tool_failure_error_function(tool, None)
        output.append(tool)
    return output


def _terminal_tool_result(
    _context: Any, results: list[FunctionToolResult], terminal: _TerminalTurnState
) -> ToolsToFinalOutputResult:
    """A confirmed terminal receipt ends this SDK turn before stale tools can be called."""
    for item in results:
        if item.tool.name not in {"report_result", "submit_completion"} or not isinstance(item.output, str):
            continue
        try:
            result = json.loads(item.output)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("success") is True and terminal.saved:
            return ToolsToFinalOutputResult(is_final_output=True, final_output="Saved terminal action.")
    return ToolsToFinalOutputResult(is_final_output=False)


async def _run(
    bound: TeamModel,
    prompt: str,
    specifications: list[dict[str, Any]],
    tools: TaskSessionTools,
    effort: str | None,
    settings: Settings,
) -> TurnOutcome:
    terminal = _TerminalTurnState()
    agent = Agent[None](
        name="OneFoundry team agent",
        instructions=TEAM_INSTRUCTIONS,
        model=bound.model,
        tools=list(_function_tools(specifications, tools, terminal)),
        tool_use_behavior=lambda context, results: _terminal_tool_result(context, results, terminal),
        model_settings=ModelSettings(
            # The ChatGPT-plan route rejects an output cap. Grok uses max_completion_tokens.
            max_tokens=(
                None if bound.provider in {"openai_codex_runtime", "xai"} else settings.team_max_output_tokens
            ),
            extra_args=(
                {"max_completion_tokens": settings.team_max_output_tokens}
                if bound.provider == "xai"
                else None
            ),
            parallel_tool_calls=False if bound.responses_api else None,
            store=False if bound.responses_api or bound.provider == "xai" else None,
            reasoning=Reasoning(effort=cast(ReasoningEffort, effort)) if effort else None,
            retry=ModelRetrySettings(max_retries=0),
        ),
    )
    run_config = RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)
    try:
        try:
            async with asyncio.timeout(settings.team_turn_timeout_seconds):
                if bound.streamed:
                    streaming = Runner.run_streamed(
                        agent, prompt, max_turns=settings.team_max_model_calls_per_turn, run_config=run_config
                    )
                    async for _event in streaming.stream_events():
                        pass
                    final, usage = streaming.final_output, streaming.context_wrapper.usage
                else:
                    result = await Runner.run(
                        agent, prompt, max_turns=settings.team_max_model_calls_per_turn, run_config=run_config
                    )
                    final, usage = result.final_output, result.context_wrapper.usage
        except TimeoutError:
            return TurnOutcome("unknown", "provider_outcome_unknown")
        except Exception as error:  # noqa: BLE001 - every failure becomes one saved outcome
            return classify_failure(error)
    finally:
        if bound.client is not None:
            await bound.client.close()
    used_in, used_out = usage.input_tokens, usage.output_tokens
    if terminal.saved:
        return TurnOutcome("done", input_tokens=used_in, output_tokens=used_out)
    if (
        not isinstance(final, str)
        or not final.strip()
        or len(final) > 16000
        or contains_sensitive_text(final)
    ):
        return TurnOutcome("failed", "provider_reply_invalid", input_tokens=used_in, output_tokens=used_out)
    try:
        reply = local_text(final)
    except ValueError:
        return TurnOutcome("failed", "provider_reply_invalid", input_tokens=used_in, output_tokens=used_out)
    return TurnOutcome("done", reply=reply, input_tokens=used_in, output_tokens=used_out)


class TaskSessionSdkTurn:
    """In-app team worker for text agents; coding work stays in the Execution Gateway."""

    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        *,
        model_factory: ModelFactory = team_model,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.secrets = ModelSecretStore(settings.model_secret_root)
        self.model_factory = model_factory
        self.attempts = TaskSessionAttempts(gateway)

    def run_next(self, project_id: UUID | None = None) -> dict[str, Any] | None:
        claimed = self.attempts.claim_next(project_id)
        if claimed is None:
            return None
        return self.run_claimed(claimed["project_id"], claimed)

    def run_claimed(self, project_id: UUID, claimed: Mapping[str, Any]) -> dict[str, Any]:
        attempt_id, session_id = claimed["id"], claimed["session_id"]
        try:
            with self.gateway.engine.connect() as connection:
                metadata = configured_profile(connection, project_id, claimed["profile_id"])
                blockers = profile_blockers(metadata, self.settings)
                if blockers:
                    raise TeamModelUnavailable("; ".join(blockers))
                runtime = resolve(connection, project_id, claimed["profile_id"], self.secrets)
                effort = connection.scalar(
                    text("SELECT reasoning_effort FROM profiles WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": claimed["profile_id"]},
                )
            bound = self.model_factory(runtime, self.settings)
        except (Conflict, NotFound, TeamModelUnavailable, InvalidRequestError, ValueError):
            # Nothing was sent; the operator must fix the agent's model account.
            return self.attempts.settle(project_id, attempt_id, "failed", reason="agent_model_unavailable")
        try:
            bind(self.gateway, project_id, claimed)
            tools = TaskSessionTools(self.gateway, project_id, session_id, attempt_id)
            tools.reconcile_previous_actions()
            snapshot = TaskSessionContext(self.gateway, self.settings).prepare(project_id, attempt_id)
            specifications = list(tools.specifications()[0]["tools"])
        except (Conflict, NotFound, Unavailable, ValueError) as error:
            if bound.client is not None:
                asyncio.run(bound.client.close())
            if isinstance(error, TeamContextBudgetExceeded):
                return self.attempts.settle(
                    project_id,
                    attempt_id,
                    "failed",
                    reason="team_context_budget_exceeded",
                )
            if isinstance(error, Unavailable):
                reason = str(error)
                return self.attempts.settle(
                    project_id,
                    attempt_id,
                    "failed",
                    reason=reason if reason in MEMORY_UNAVAILABLE_REASONS else "runner_preflight_unavailable",
                )
            return self.attempts.settle(project_id, attempt_id, "failed", reason="runner_preflight_failed")
        agents_debug.DONT_LOG_MODEL_DATA = True
        agents_debug.DONT_LOG_TOOL_DATA = True
        set_tracing_disabled(True)
        outcome = asyncio.run(_run(bound, snapshot.prompt, specifications, tools, effort, self.settings))
        return self.attempts.settle(
            project_id,
            attempt_id,
            cast(Any, outcome.status),
            reason=outcome.reason,
            public_reply=outcome.reply,
            provider=bound.provider,
            model_id=bound.model_id,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            retryable=outcome.retryable,
        )
