"""One claimed team turn over an admitted Codex app-server connection.

This is a runner-side turn adapter, not an app worker or a CLI launcher. The
caller must supply an isolated app-server factory and an admitted repository.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from onefoundry.gateway_errors import Conflict
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.workflow_schemas import local_text
from runner.codex_app_server import (
    AppServerError,
    AppServerThreadNotFound,
    AppServerTimeout,
    CodexAppServer,
)

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

ClientFactory = Callable[[Callable[[str, Mapping[str, Any], str], Mapping[str, Any]]], CodexAppServer]


def _admitted_names(specifications: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for namespace in specifications:
        if namespace.get("type") != "namespace" or namespace.get("name") != "onefoundry":
            raise ValueError("unexpected team tool namespace")
        for tool in namespace["tools"]:
            names.append(f"onefoundry.{tool['name']}")
    return names


def _public_message(
    event: Mapping[str, Any], thread_id: str, turn_id: str
) -> tuple[str | None, str | None, str | None]:
    if event.get("method") != "item/completed":
        return None, None, None
    params = event.get("params")
    if not isinstance(params, dict) or params.get("threadId") != thread_id or params.get("turnId") != turn_id:
        return None, None, None
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") != "agentMessage":
        return None, None, None
    body, phase, item_id = item.get("text"), item.get("phase"), item.get("id")
    if not isinstance(body, str) or phase not in (None, "commentary", "final_answer"):
        return None, None, None
    return body, phase, item_id if isinstance(item_id, str) else None


class TaskSessionCodexTurn:
    def __init__(self, gateway: ToolGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings
        self.attempts = TaskSessionAttempts(gateway)

    def run_claimed(
        self,
        project_id: UUID,
        claimed: Mapping[str, Any],
        *,
        repository: Path,
        model_id: str,
        reasoning_effort: str | None = None,
        client_factory: ClientFactory,
    ) -> dict[str, Any]:
        """Run exactly one already-claimed turn and settle only its observed outcome."""
        attempt_id, session_id = claimed["id"], claimed["session_id"]
        if not isinstance(attempt_id, UUID) or not isinstance(session_id, UUID):
            raise ValueError("invalid claimed team attempt")
        try:
            if not repository.is_absolute() or not repository.is_dir() or repository.is_symlink():
                raise ValueError("invalid admitted repository")
            if not model_id or any(char.isspace() for char in model_id):
                raise ValueError("invalid selected model")
            tools = TaskSessionTools(self.gateway, project_id, session_id, attempt_id)
            tools.reconcile_previous_actions()
            snapshot = TaskSessionContext(self.gateway, self.settings).prepare(project_id, attempt_id)
            specifications = tools.specifications()
            thread_id = claimed.get("provider_thread_id")
            if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
                raise ValueError("invalid saved provider thread")
        except (Conflict, ValueError):
            return self.attempts.settle(project_id, attempt_id, "failed", reason="runner_preflight_failed")
        turn_id: str | None = None
        turn_launch_requested = False
        final_message: str | None = None
        fallback_message: str | None = None
        public_update_ids: set[str] = set()
        input_tokens: int | None = None
        output_tokens: int | None = None
        started = time.monotonic()
        try:
            with client_factory(tools.invoke) as client:
                # A configured Responses provider uses the OneFoundry OAuth token;
                # app-server account/read can be empty even when that provider works.
                # The isolated factory must admit the credential before this call.
                if thread_id is None:
                    thread_id = client.start_thread(
                        repository=repository, model=model_id, dynamic_tools=specifications
                    )
                else:
                    try:
                        client.resume_thread(
                            thread_id,
                            repository=repository,
                            admitted_dynamic_tools=_admitted_names(specifications),
                        )
                    except AppServerThreadNotFound:
                        self.attempts.replace_missing_thread(project_id, attempt_id, thread_id)
                        thread_id = client.start_thread(
                            repository=repository, model=model_id, dynamic_tools=specifications
                        )
                self.attempts.bind_provider_thread(project_id, attempt_id, thread_id)
                # A lost turn/start response can still mean Codex accepted the turn.
                turn_launch_requested = True
                turn_id = client.start_turn(snapshot.prompt, repository=repository, effort=reasoning_effort)
                self.attempts.bind_provider_turn(project_id, attempt_id, turn_id)
                while True:
                    remaining = self.settings.onefoundry_max_elapsed_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise AppServerTimeout("team turn elapsed bound reached")
                    try:
                        event = client.next_event(timeout_seconds=min(remaining, 30))
                    except AppServerTimeout:
                        continue
                    body, phase, item_id = _public_message(event, thread_id, turn_id)
                    if body is not None:
                        if phase == "final_answer":
                            final_message = body
                        elif phase == "commentary" and item_id is not None:
                            if item_id not in public_update_ids and len(public_update_ids) < 8:
                                self.attempts.save_public_update(project_id, attempt_id, item_id, body)
                                public_update_ids.add(item_id)
                        elif phase is None:
                            fallback_message = body
                    if event.get("method") == "thread/tokenUsage/updated":
                        params = event.get("params")
                        if (
                            isinstance(params, dict)
                            and params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                        ):
                            usage = params.get("tokenUsage")
                            last = usage.get("last") if isinstance(usage, dict) else None
                            if isinstance(last, dict):
                                incoming, outgoing = last.get("inputTokens"), last.get("outputTokens")
                                if type(incoming) is int and incoming >= 0:
                                    input_tokens = incoming
                                if type(outgoing) is int and outgoing >= 0:
                                    output_tokens = outgoing
                    if event.get("method") != "turn/completed":
                        continue
                    params = event.get("params")
                    turn = params.get("turn") if isinstance(params, dict) else None
                    status = turn.get("status") if isinstance(turn, dict) else None
                    if status == "completed":
                        reply = final_message or fallback_message
                        if reply is None or len(reply) > 16000 or contains_sensitive_text(reply):
                            return self.attempts.settle(
                                project_id,
                                attempt_id,
                                "failed",
                                reason="provider_reply_invalid",
                                provider="openai_codex_runtime",
                                model_id=model_id,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            )
                        try:
                            reply = local_text(reply)
                        except ValueError:
                            return self.attempts.settle(
                                project_id,
                                attempt_id,
                                "failed",
                                reason="provider_reply_invalid",
                                provider="openai_codex_runtime",
                                model_id=model_id,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            )
                        return self.attempts.settle(
                            project_id,
                            attempt_id,
                            "done",
                            public_reply=reply,
                            provider="openai_codex_runtime",
                            model_id=model_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    if status in ("failed", "interrupted"):
                        reason = "provider_turn_failed" if status == "failed" else "provider_turn_interrupted"
                        return self.attempts.settle(
                            project_id,
                            attempt_id,
                            "failed",
                            reason=reason,
                            provider="openai_codex_runtime",
                            model_id=model_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    raise Conflict("provider terminal event is invalid")
        except AppServerError:
            return self.attempts.settle(
                project_id,
                attempt_id,
                "unknown" if turn_launch_requested else "failed",
                reason="provider_outcome_unknown" if turn_launch_requested else "provider_unavailable",
                provider="openai_codex_runtime",
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                retryable=not turn_launch_requested,
            )
        except (Conflict, ValueError):
            return self.attempts.settle(
                project_id,
                attempt_id,
                "unknown" if turn_launch_requested else "failed",
                reason="team_tool_outcome_unknown" if turn_launch_requested else "runner_preflight_failed",
                provider="openai_codex_runtime",
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
