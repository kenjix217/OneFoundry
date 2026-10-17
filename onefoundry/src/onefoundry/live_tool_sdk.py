"""Bounded SDK continuation for an OpenAI API-key text role with admitted tools.

The installed Agents SDK owns model/tool turns. OneFoundry's Tool Gateway remains
the only authority for each tool call; SDK tool names confer no permission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid4

from agents import (
    Agent,
    FunctionTool,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
    RunHooks,
    Runner,
    Tool,
    set_tracing_disabled,
)
from agents import _debug as agents_debug
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.models.openai_responses import OpenAIResponsesModel
from agents.run_context import RunContextWrapper
from agents.tool import set_function_tool_failure_error_function
from agents.tool_context import ToolContext
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from openai.types.shared import Reasoning, ReasoningEffort

from onefoundry.inference import IncompleteResponseError, InvalidRequestError, TextResult
from onefoundry.inference.responses import _cloud_endpoint
from onefoundry.role_tool_admission import (
    LIVE_SDK_EXCLUDED_IDS,
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolPolicyRejected,
    ToolTimedOut,
)

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class PreparedToolRun(Protocol):
    @property
    def run_id(self) -> UUID: ...

    @property
    def order_id(self) -> UUID: ...

    @property
    def profile_id(self) -> UUID: ...

    @property
    def provider(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def api_key(self) -> str | None: ...

    @property
    def instructions(self) -> str: ...

    @property
    def prompt(self) -> str: ...


class ToolSdkSettings(Protocol):
    live_text_openai_base_url: str
    live_text_max_output_tokens: int
    live_text_timeout_seconds: int
    live_text_tool_max_turns: int


class _AttemptHooks(RunHooks[None]):
    def __init__(self, gateway: ToolGateway, project_id: UUID, prepared: PreparedToolRun) -> None:
        self.gateway = gateway
        self.project_id = project_id
        self.prepared = prepared
        self.index = 0
        self.pending: UUID | None = None

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        with self.gateway.engine.begin() as connection:
            self.gateway._event(
                connection,
                self.project_id,
                self.prepared.order_id,
                self.prepared.run_id,
                event_type,
                payload,
                actor_id=f"profile:{self.prepared.profile_id}",
            )

    async def on_llm_start(
        self,
        context: RunContextWrapper[None],
        agent: Agent[None],
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        if self.pending is not None:
            raise RuntimeError("concurrent model attempts are unavailable")
        self.index += 1
        attempt_id = uuid4()
        encoded = json.dumps(
            {"system_prompt": system_prompt, "input_items": input_items},
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._record(
            "live_model_call_started",
            {
                "attempt_id": str(attempt_id),
                "attempt_index": self.index,
                "model_id": self.prepared.model_id,
                "sdk_input_sha256": hashlib.sha256(encoded).hexdigest(),
                "sdk_input_bytes": len(encoded),
            },
        )
        self.pending = attempt_id

    async def on_llm_end(
        self, context: RunContextWrapper[None], agent: Agent[None], response: ModelResponse
    ) -> None:
        if self.pending is None:
            raise RuntimeError("model attempt has no start receipt")
        payload: dict[str, Any] = {
            "attempt_id": str(self.pending),
            "attempt_index": self.index,
            "status": "done",
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "response_id_sha256": hashlib.sha256((response.response_id or "").encode()).hexdigest(),
        }
        self._record("live_model_call_finished", payload)
        self.pending = None

    def record_unknown(self) -> None:
        if self.pending is not None:
            self._record(
                "live_model_call_finished",
                {"attempt_id": str(self.pending), "attempt_index": self.index, "status": "unknown"},
            )
            self.pending = None


_TOOL_FAILURES = (
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolPolicyRejected,
    ToolTimedOut,
)


def _gateway_failure(error: BaseException) -> BaseException | None:
    """Unwrap SDK UserError without converting a Tool Gateway failure to text."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            return None
        if isinstance(current, _TOOL_FAILURES):
            return current
        current = current.__cause__
    return None


def _tools(
    gateway: ToolGateway,
    project_id: UUID,
    prepared: PreparedToolRun,
    maximum_calls: int,
) -> list[Tool]:
    # Raw source/diff text requires exact declassification; PR reads require
    # approval wait/resume. Trusted run-bound callers may use those tools.
    advertised = [
        item
        for item in gateway.available_role_tools(project_id, prepared.run_id, prepared.profile_id)
        if item["id"] not in LIVE_SDK_EXCLUDED_IDS
    ]
    if not advertised:
        raise ToolDenied("live tool route has no admitted tools")
    lock = asyncio.Lock()
    count = 0
    output: list[Tool] = []
    for descriptor in advertised:
        tool_id = descriptor["id"]
        version = descriptor["version"]

        async def invoke(
            context: ToolContext[Any], raw: str, *, _tool_id: str = tool_id, _version: int = version
        ) -> str:
            nonlocal count
            async with lock:
                count += 1
                if count > maximum_calls:
                    raise ToolDenied("live text tool call limit reached")
                try:
                    arguments: object = json.loads(raw)
                except ValueError:
                    arguments = raw
                receipt = await asyncio.to_thread(
                    gateway.invoke_role_tool,
                    project_id,
                    prepared.run_id,
                    prepared.profile_id,
                    context.tool_call_id,
                    _tool_id,
                    arguments,
                    tool_version=_version,
                )
                return json.dumps(receipt["result"], allow_nan=False, separators=(",", ":"))

        tool = FunctionTool(
            name=tool_id,
            description=descriptor["description"],
            params_json_schema=descriptor["parameters"],
            on_invoke_tool=invoke,
            strict_json_schema=True,
        )
        set_function_tool_failure_error_function(tool, None)
        output.append(tool)
    return output


async def _run_async(
    gateway: ToolGateway,
    project_id: UUID,
    prepared: PreparedToolRun,
    settings: ToolSdkSettings,
    model: Model | None,
) -> TextResult:
    client: AsyncOpenAI | None = None
    http_client: DefaultAsyncHttpxClient | None = None
    try:
        if model is None:
            if prepared.api_key is None:
                raise InvalidRequestError("OpenAI API credential is unavailable")
            _cloud_endpoint(settings.live_text_openai_base_url, "api.openai.com")
            http_client = DefaultAsyncHttpxClient(
                trust_env=False,
                follow_redirects=False,
                timeout=settings.live_text_timeout_seconds,
            )
            client = AsyncOpenAI(
                api_key=prepared.api_key,
                base_url=settings.live_text_openai_base_url,
                max_retries=0,
                timeout=settings.live_text_timeout_seconds,
                http_client=http_client,
            )
            model = OpenAIResponsesModel(prepared.model_id, client)
        tools = _tools(gateway, project_id, prepared, settings.live_text_tool_max_turns)
        hooks = _AttemptHooks(gateway, project_id, prepared)
        configured_effort: str | None = getattr(prepared, "reasoning_effort", None)
        reasoning = Reasoning(effort=cast(ReasoningEffort, configured_effort)) if configured_effort else None
        agent = Agent[None](
            name="OneFoundry text role",
            instructions=prepared.instructions,
            model=model,
            tools=tools,
            model_settings=ModelSettings(
                max_tokens=settings.live_text_max_output_tokens,
                parallel_tool_calls=False,
                store=False,
                timeout=settings.live_text_timeout_seconds,
                reasoning=reasoning,
                retry=ModelRetrySettings(max_retries=0),
            ),
        )
        try:
            async with asyncio.timeout(settings.live_text_timeout_seconds):
                result = await Runner.run(
                    agent,
                    prepared.prompt,
                    max_turns=settings.live_text_tool_max_turns,
                    hooks=hooks,
                    run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
                )
        except BaseException as error:
            hooks.record_unknown()
            gateway_failure = _gateway_failure(error)
            if gateway_failure is not None:
                raise gateway_failure from None
            raise
        if not isinstance(result.final_output, str) or not result.final_output.strip():
            raise IncompleteResponseError("provider returned no final text")
        if not result.raw_responses or not result.raw_responses[-1].response_id:
            raise IncompleteResponseError("provider returned no final response id")
        usage = result.context_wrapper.usage
        return TextResult(
            result.final_output,
            result.raw_responses[-1].response_id,
            usage.input_tokens,
            usage.output_tokens,
            "completed",
        )
    finally:
        if client is not None:
            await client.close()
        elif http_client is not None:
            await http_client.aclose()


def run_live_tool_sdk(
    gateway: ToolGateway,
    project_id: UUID,
    prepared: PreparedToolRun,
    settings: ToolSdkSettings,
    *,
    model: Model | None = None,
) -> TextResult:
    """Run one bounded SDK continuation; callers own run terminal persistence."""
    if prepared.provider != "openai" or not prepared.api_key:
        raise ValueError("live tools require an OpenAI API-key account")
    if not 1 <= settings.live_text_tool_max_turns <= 8:
        raise ValueError("live tool turn limit is invalid")
    agents_debug.DONT_LOG_MODEL_DATA = True
    agents_debug.DONT_LOG_TOOL_DATA = True
    set_tracing_disabled(True)
    return asyncio.run(_run_async(gateway, project_id, prepared, settings, model))
