"""One-shot live text work, owned by ToolGateway and recorded before egress."""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry import memory_store, run_audit, task_automation
from onefoundry.codex_account import CodexConnectionError, decode_state
from onefoundry.grok_account import GrokConnectionError
from onefoundry.grok_account import decode_state as decode_grok_state
from onefoundry.inference import (
    AuthenticationError,
    CompatibleChatTransport,
    IncompleteResponseError,
    InvalidRequestError,
    OllamaTextTransport,
    OpenAIResponsesTransport,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
    TransportError,
    XAIResponsesTransport,
)
from onefoundry.inference.contracts import InputImage
from onefoundry.inference.native_cloud import AnthropicMessagesTransport, GeminiGenerateContentTransport
from onefoundry.inference.subscription_codex import DEFAULT_ENDPOINT as CODEX_ENDPOINT
from onefoundry.inference.subscription_codex import (
    CodexAccountTransport,
    CodexStreamFailure,
    CodexStreamIncomplete,
)
from onefoundry.inference.subscription_grok import GrokAccountTransport
from onefoundry.input_file_context import ContextSelection, select_context, verified_images
from onefoundry.live_text_schemas import LiveTextRestart
from onefoundry.live_tool_readiness import live_tool_readiness
from onefoundry.memory_policy import work_memory_query
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.profile_hierarchy import lock_project
from onefoundry.provider_registry import provider_spec
from onefoundry.settings import Settings
from onefoundry.skill_store import select_procedures
from onefoundry.tool_gateway import Conflict, NotFound
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class TextTransport(Protocol):
    def generate(self, request: TextRequest) -> TextResult: ...


class LiveTextFailure(Exception):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id


class LiveTextUnknown(Exception):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id


class RunPreflightBlocked(Conflict):
    """Required role capability or configuration is absent before provider launch."""


def _diagnostic_code(error: Exception) -> str:
    """Return a fixed code without persisting provider bodies or exception text."""
    if isinstance(error, (CodexStreamFailure, CodexStreamIncomplete)):
        return error.diagnostic_code
    if isinstance(error, AuthenticationError):
        return "authentication_rejected"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, InvalidRequestError):
        return "invalid_request"
    if isinstance(error, IncompleteResponseError):
        return "incomplete_response"
    if isinstance(error, TransportError):
        return "transport_error"
    if isinstance(error, ProviderError):
        match = re.fullmatch(r"Codex account request failed with HTTP ([1-5][0-9]{2})", str(error))
        return f"provider_http_{match.group(1)}" if match else "provider_error"
    return "unexpected_error"


@dataclass(frozen=True)
class Prepared:
    run_id: UUID
    order_id: UUID
    profile_id: UUID
    account_id: UUID
    provider: str
    model_id: str
    credential_revision: int
    endpoint: str | None
    api_key: str | None
    instructions: str
    prompt: str
    memories: list[dict[str, Any]]
    codex_state: dict[str, Any] | None = None
    grok_state: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    tools_active: bool = False
    images: tuple[InputImage, ...] = ()
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None


def _provider_transport(prepared: Prepared, settings: Settings) -> TextTransport:
    if prepared.provider == "openai_codex_runtime" and prepared.codex_state is not None:
        return CodexAccountTransport(
            endpoint=CODEX_ENDPOINT,
            token=prepared.codex_state["access_token"],
            account_id=prepared.codex_state["account_id"],
        )
    if prepared.provider == "xai" and prepared.grok_state is not None:
        return GrokAccountTransport(
            endpoint=settings.grok_account_base_url + "/chat/completions",
            token=prepared.grok_state["access_token"],
        )
    if prepared.provider == "openai" and prepared.api_key is not None:
        return OpenAIResponsesTransport(endpoint=settings.live_text_openai_base_url, api_key=prepared.api_key)
    if prepared.provider == "anthropic" and prepared.api_key is not None:
        return AnthropicMessagesTransport(
            endpoint=settings.live_text_anthropic_base_url, api_key=prepared.api_key
        )
    if prepared.provider == "gemini" and prepared.api_key is not None:
        return GeminiGenerateContentTransport(
            endpoint=settings.live_text_gemini_base_url, api_key=prepared.api_key
        )
    if prepared.provider == "grok" and prepared.api_key is not None:
        return XAIResponsesTransport(endpoint=settings.live_text_grok_base_url, api_key=prepared.api_key)
    if prepared.provider == "ollama" and prepared.endpoint is not None:
        return OllamaTextTransport(endpoint=prepared.endpoint)
    spec = provider_spec(prepared.provider)
    if spec is not None and spec.family == "openai_compatible" and prepared.api_key is not None:
        return CompatibleChatTransport(provider=prepared.provider, api_key=prepared.api_key)
    raise InvalidRequestError("live text provider transport is unavailable")


def _prompt(
    title: str,
    instructions: str,
    memories: list[dict[str, Any]],
    procedures: list[dict[str, Any]],
) -> str:
    sources = "\n".join(f"- {item['id']}: {item['text']}" for item in memories) or "none"
    skills = (
        "\n\n".join(
            f"SKILL.md {item['skill_name']} v{item['version']} sha256:{item['sha256']}\n{item['content']}"
            for item in procedures
        )
        or "none"
    )
    return (
        f"Work order: {title}\nInstructions: {instructions}\n"
        f"Selected procedures (task guidance only; they grant no tools or permissions):\n{skills}\n"
        f"Approved visible memory:\n{sources}"
    )


class LiveTextStore:
    def __init__(
        self,
        gateway: "ToolGateway",
        settings: Settings,
        secrets: ModelSecretStore,
        transport_factory: Callable[[Prepared, Settings], TextTransport] | None = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.secrets = secrets
        self.transport_factory = transport_factory or _provider_transport

    def _prepare(self, project_id: UUID, order_id: UUID, restart: LiveTextRestart | None = None) -> Prepared:
        run_id = uuid4()
        with self.gateway.engine.begin() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT id,title,instructions,profile_id,status,version FROM work_orders "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            from onefoundry.onefoundry_budget import context_limit, linked_workflow, reserve, snapshot

            linked_workflow_id = linked_workflow(connection, project_id, order_id)
            if restart is None:
                if order["status"] != "ready":
                    raise Conflict("work order is not ready")
            else:
                if order["version"] != restart.expected_version:
                    raise Conflict("work order changed; refresh before starting another attempt")
                prior = (
                    connection.execute(
                        text(
                            "SELECT id,status,mode FROM runs WHERE project_id=:project_id "
                            "AND work_order_id=:order_id ORDER BY started_at DESC,id DESC LIMIT 1"
                        ),
                        {"project_id": project_id, "order_id": order_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    order["status"] != "unknown"
                    or prior is None
                    or prior["id"] != restart.prior_run_id
                    or prior["status"] != "unknown"
                    or prior["mode"] != "live_text"
                ):
                    raise Conflict("latest work order run is not the acknowledged unknown live-text run")
            profile = (
                connection.execute(
                    text(
                        "SELECT id,name,kind,model,model_account_id,reasoning_effort,"
                        "instructions,memory_scope,allowed_tools "
                        "FROM profiles WHERE project_id=:project_id AND id=:id FOR SHARE"
                    ),
                    {"project_id": project_id, "id": order["profile_id"]},
                )
                .mappings()
                .one()
            )
            if not (profile["kind"] == "custom" or profile["name"] in ("Researcher", "GTM")):
                raise RunPreflightBlocked("profile is not a live text role")
            if not profile["model"] or not profile["model_account_id"] or not profile["instructions"].strip():
                raise RunPreflightBlocked("profile model and instructions are required")
            try:
                safe_instructions = local_text(str(profile["instructions"]))
            except ValueError:
                raise RunPreflightBlocked("profile instructions are unavailable for live text") from None
            safe_instructions += "\n\n" + task_automation.DECISION_INSTRUCTIONS
            automation_context = task_automation.decision_context(
                connection, project_id, profile["id"], self.settings
            )
            account = (
                connection.execute(
                    text(
                        "SELECT id,provider,auth_kind,endpoint,catalog_valid,"
                        "credential_ready,credential_revision "
                        "FROM model_accounts WHERE project_id=:project_id AND id=:id FOR SHARE"
                    ),
                    {"project_id": project_id, "id": profile["model_account_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if account is None:
                raise RunPreflightBlocked("selected account has no live text transport")
            spec = provider_spec(str(account["provider"]))
            if spec is None or not spec.text_runtime_available:
                raise RunPreflightBlocked("selected account has no live text transport")
            if not account["catalog_valid"]:
                raise RunPreflightBlocked("selected model catalog is unavailable")
            model_efforts = connection.scalar(
                text(
                    "SELECT reasoning_efforts FROM model_catalog WHERE project_id=:project_id "
                    "AND account_id=:account_id AND model_id=:model_id AND category='text'"
                ),
                {
                    "project_id": project_id,
                    "account_id": account["id"],
                    "model_id": profile["model"],
                },
            )
            if model_efforts is None or (
                profile["reasoning_effort"] is not None and profile["reasoning_effort"] not in model_efforts
            ):
                raise RunPreflightBlocked(
                    "selected model or reasoning effort is no longer in the discovered catalog"
                )
            api_key: str | None = None
            codex_state: dict[str, Any] | None = None
            grok_state: dict[str, Any] | None = None
            if account["auth_kind"] == "api_key":
                if not account["credential_ready"]:
                    raise RunPreflightBlocked("model credential unavailable")
                try:
                    api_key = self.secrets.read(account["id"])
                except SecretStoreError:
                    raise RunPreflightBlocked("model credential unavailable") from None
            elif account["auth_kind"] == "account_runtime" and account["provider"] == "openai_codex_runtime":
                if not account["credential_ready"]:
                    raise RunPreflightBlocked("Codex account is not connected")
                try:
                    codex_state = decode_state(self.secrets.read(account["id"]))
                except (SecretStoreError, CodexConnectionError):
                    raise RunPreflightBlocked("Codex credential unavailable") from None
                codex_expiry = codex_state.get("expires_at")
                if (
                    codex_state.get("kind") != "connected"
                    or isinstance(codex_expiry, int)
                    and codex_expiry <= time.time() + 60
                ):
                    raise RunPreflightBlocked("Codex credential expired; refresh models to renew it")
            elif account["auth_kind"] == "account_runtime" and account["provider"] == "xai":
                if not account["credential_ready"]:
                    raise RunPreflightBlocked("Grok account is not connected")
                try:
                    grok_state = decode_grok_state(self.secrets.read(account["id"]))
                except (SecretStoreError, GrokConnectionError):
                    raise RunPreflightBlocked("Grok credential unavailable") from None
                if (
                    grok_state.get("kind") != "connected"
                    or grok_state.get("expires_at", 0) <= time.time() + 60
                ):
                    raise RunPreflightBlocked("Grok credential expired; refresh models to renew it")
            elif account["auth_kind"] != "none" or account["provider"] != "ollama":
                raise RunPreflightBlocked("model account authentication is invalid")
            memory_query = work_memory_query(str(order["title"]), str(order["instructions"]))
            scope = "project" if profile["memory_scope"] == "project" else None
            memories = (
                []
                if linked_workflow_id is not None
                else memory_store.retrieve_memory(
                    connection, project_id, memory_query, 5, scope, self.settings
                )
            )
            base_bytes = len(safe_instructions.encode("utf-8")) + len(
                (
                    _prompt(str(order["title"]), str(order["instructions"]), memories, [])
                    + "\n\n"
                    + automation_context
                ).encode("utf-8")
            )
            available_content_bytes = max(
                0,
                min(
                    self.settings.skill_auto_max_content_bytes,
                    self.settings.live_text_max_prompt_bytes - base_bytes - 512,
                ),
            )
            procedures = select_procedures(
                connection,
                project_id,
                profile["id"],
                str(order["title"]),
                str(order["instructions"]),
                max_auto=self.settings.skill_auto_max_count,
                max_content_bytes=available_content_bytes,
            )
            while True:
                prompt = (
                    _prompt(str(order["title"]), str(order["instructions"]), memories, procedures)
                    + "\n\n"
                    + automation_context
                )
                try:
                    prompt = local_text(prompt)
                except ValueError:
                    raise Conflict("live text prompt is unavailable") from None
                if (
                    len(safe_instructions.encode("utf-8")) + len(prompt.encode("utf-8"))
                    <= self.settings.live_text_max_prompt_bytes
                ):
                    break
                if not procedures or procedures[-1]["selection_source"] != "automatic":
                    raise Conflict("live text prompt exceeds configured limit")
                procedures.pop()
            file_budget = min(
                12000,
                self.settings.live_text_max_prompt_bytes
                - len(safe_instructions.encode("utf-8"))
                - len(prompt.encode("utf-8"))
                - 512,
            )
            selected_files = (
                ContextSelection("", (), 0, ())
                if linked_workflow_id is not None
                else select_context(
                    connection,
                    project_id,
                    order_id,
                    [],
                    str(order["instructions"]),
                    file_budget,
                    self.settings.input_file_max_link_count,
                    allow_visual=account["provider"] in {"openai", "openai_codex_runtime"},
                    max_visual_bytes=self.settings.input_file_max_visual_bytes,
                    max_visual_count=self.settings.input_file_max_visual_count,
                )
            )
            if selected_files.text:
                prompt += "\n\n" + selected_files.text
            if (
                len(safe_instructions.encode("utf-8")) + len(prompt.encode("utf-8"))
                > self.settings.live_text_max_prompt_bytes
            ):
                raise Conflict("live text prompt exceeds configured limit with attached files")
            tools_active, tool_error = live_tool_readiness(
                profile["allowed_tools"],
                provider=account["provider"],
                api_key_present=api_key is not None,
                role_name=profile["name"],
                role_kind=profile["kind"],
                has_visuals=bool(selected_files.visuals),
                settings=self.settings,
            )
            if tool_error is not None:
                raise RunPreflightBlocked(tool_error)
            linked_output_cap: int | None = None
            linked_timeout: int | None = None
            if linked_workflow_id is not None:
                policy = snapshot(connection, self.gateway, project_id, linked_workflow_id)
                byte_limit = context_limit(self.settings, policy)
                if (
                    byte_limit is not None
                    and len(safe_instructions.encode("utf-8")) + len(prompt.encode("utf-8")) > byte_limit
                ):
                    raise Conflict("budget_exceeded: linked prompt exceeds context byte limit")
                linked_output_cap = policy["output_tokens_limit"]
                elapsed = connection.scalar(
                    text("SELECT EXTRACT(EPOCH FROM (clock_timestamp()-:started))"),
                    {"started": policy["created_at"]},
                )
                linked_timeout = min(
                    self.settings.live_text_timeout_seconds,
                    int(policy["elapsed_seconds_limit"] - elapsed),
                )
                if linked_timeout < 1:
                    raise Conflict("budget_exceeded: insufficient elapsed time for a model attempt")
                reserve(
                    connection,
                    self.gateway,
                    project_id,
                    linked_workflow_id,
                    f"model_attempt:{run_id}",
                    "model_attempt",
                )
            run_audit.insert_run(
                connection,
                project_id,
                run_id,
                order_id,
                profile["id"],
                self.gateway.actor_id,
                "live_text",
                procedures=procedures,
                extension_worker_image=self.settings.extension_worker_image,
            )
            if restart is not None:
                self.gateway._event(
                    connection,
                    project_id,
                    order_id,
                    run_id,
                    "live_text_restart_requested",
                    {"prior_run_id": str(restart.prior_run_id), "acknowledged_unknown": True},
                )
            for use in selected_files.uses:
                connection.execute(
                    text(
                        "INSERT INTO input_file_context_uses "
                        "(id,project_id,actor_id,run_id,file_id,chunk_id,kind,"
                        "source_label,sha256,byte_count) VALUES "
                        "(:id,:project_id,:actor_id,:run_id,:file_id,:chunk_id,'text',:label,:sha,:size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "run_id": run_id,
                        "file_id": use.file_id,
                        "chunk_id": use.chunk_id,
                        "label": use.source_label,
                        "sha": use.chunk_sha256,
                        "size": use.byte_count,
                    },
                )
            for visual in selected_files.visuals:
                connection.execute(
                    text(
                        "INSERT INTO input_file_context_uses "
                        "(id,project_id,actor_id,run_id,file_id,kind,source_label,sha256,byte_count) "
                        "VALUES (:id,:project_id,:actor_id,:run_id,:file_id,'visual',:label,:sha,:size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "run_id": run_id,
                        "file_id": visual.file_id,
                        "label": visual.file_name,
                        "sha": visual.file_sha256,
                        "size": visual.byte_size,
                    },
                )
            images = verified_images(self.settings.input_file_root, project_id, selected_files.visuals)
            prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
            connection.execute(
                text(
                    "INSERT INTO live_text_run_bindings "
                    "(id,project_id,actor_id,run_id,account_id,provider,model_id,"
                    "credential_revision,prompt_sha256,reasoning_effort) "
                    "VALUES (:id,:project_id,:actor_id,:run_id,:account_id,:provider,"
                    ":model_id,:revision,:digest,:effort)"
                ),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "run_id": run_id,
                    "account_id": account["id"],
                    "provider": account["provider"],
                    "model_id": profile["model"],
                    "revision": account["credential_revision"],
                    "digest": prompt_digest,
                    "effort": profile["reasoning_effort"],
                },
            )
            connection.execute(
                text("UPDATE work_orders SET status='running' WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": order_id},
            )
            self.gateway._event(
                connection,
                project_id,
                order_id,
                run_id,
                "live_text_started",
                {
                    "request_id": str(run_id),
                    "profile_id": str(profile["id"]),
                    "account_id": str(account["id"]),
                    "provider": account["provider"],
                    "model": profile["model"],
                    "reasoning_effort": profile["reasoning_effort"],
                    "credential_revision": account["credential_revision"],
                    "prompt_sha256": prompt_digest,
                    "input_sections": [
                        {
                            "file_id": str(use.file_id),
                            "source_label": use.source_label,
                            "sha256": use.chunk_sha256,
                        }
                        for use in selected_files.uses
                    ],
                    "input_sections_omitted": selected_files.omitted_chunks,
                    "skills": [
                        {
                            "skill_id": str(item["skill_id"]),
                            "version": item["version"],
                            "sha256": item["sha256"],
                            "selection_source": item["selection_source"],
                        }
                        for item in procedures
                    ],
                    "tools_active": tools_active,
                    "cost_usd": None,
                },
                actor_id=self.gateway.actor_id,
            )
            return Prepared(
                run_id,
                order_id,
                profile["id"],
                account["id"],
                account["provider"],
                profile["model"],
                account["credential_revision"],
                account["endpoint"],
                api_key,
                safe_instructions,
                prompt,
                memories,
                codex_state,
                grok_state,
                profile["reasoning_effort"],
                tools_active,
                images,
                max_output_tokens=linked_output_cap,
                timeout_seconds=linked_timeout,
            )

    def _output(self, connection: Any, project_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        row = (
            connection.execute(
                text("SELECT * FROM live_text_outputs WHERE project_id=:project_id AND run_id=:run_id"),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        result = dict(row)
        sources = (
            connection.execute(
                text(
                    "SELECT m.id,m.project_id,m.scope,m.text,m.source_result_id "
                    "FROM live_text_output_sources s "
                    "JOIN memory_items m ON m.project_id=s.source_project_id AND m.id=s.memory_item_id "
                    "WHERE s.project_id=:project_id AND s.output_id=:output_id ORDER BY s.id"
                ),
                {"project_id": project_id, "output_id": row["id"]},
            )
            .mappings()
            .all()
        )
        result["source_memories"] = [dict(source) for source in sources]
        result["source_memory_ids"] = [source["id"] for source in sources]
        decision = (
            connection.execute(
                text(
                    "SELECT decision,status,rule_id,rule_version,project_version,plan_version,"
                    "assigned_work_order_id,error_code "
                    "FROM task_automation_decisions WHERE project_id=:p AND run_id=:run"
                ),
                {"p": project_id, "run": run_id},
            )
            .mappings()
            .one_or_none()
        )
        result["automation_decision"] = dict(decision) if decision else None
        return result

    def get_output(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            output = self._output(connection, project_id, run_id)
        if output is None:
            raise NotFound("live text output not found")
        return output

    def _complete(self, project_id: UUID, prepared: Prepared, result: TextResult) -> dict[str, Any]:
        body = local_text(result.text)
        if len(body.encode("utf-8")) > self.settings.live_text_max_output_bytes:
            raise ValueError("live text output exceeds configured limit")
        if result.input_tokens + result.output_tokens > 2_147_483_647:
            raise ValueError("live text token count exceeds storage range")
        if not re.fullmatch(r"[A-Za-z0-9._:/+\-]{0,200}", result.provider_response_id):
            raise ValueError("provider response identifier is invalid")
        ticks = result.cost_usd_ticks
        if ticks is not None and (
            prepared.provider != "grok"
            or type(ticks) is not int
            or not 0 <= ticks <= 9_223_372_036_854_775_807
        ):
            raise ValueError("provider cost is invalid")
        cost_usd = Decimal(ticks) / Decimal(10_000_000_000) if ticks is not None else None
        digest = hashlib.sha256(body.encode()).hexdigest()
        actor = f"profile:{prepared.profile_id}"
        output_id = uuid4()
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            run = (
                connection.execute(
                    text("SELECT status,mode FROM runs WHERE project_id=:project_id AND id=:id FOR UPDATE"),
                    {"project_id": project_id, "id": prepared.run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] != "running" or run["mode"] != "live_text":
                raise Conflict("live text run state changed")
            body, _decision = task_automation.apply_decision(
                self.gateway,
                connection,
                project_id,
                prepared.run_id,
                prepared.order_id,
                prepared.profile_id,
                result.text,
                execution_kind="run",
            )
            body = local_text(body)
            if len(body.encode("utf-8")) > self.settings.live_text_max_output_bytes:
                raise ValueError("live text output with automation readback exceeds configured limit")
            digest = hashlib.sha256(body.encode()).hexdigest()
            connection.execute(
                text(
                    "INSERT INTO live_text_outputs "
                    "(id,project_id,actor_id,run_id,profile_id,account_id,provider,model_id,credential_revision,"
                    "text,text_sha256,input_tokens,output_tokens,cost_usd,cost_usd_ticks,"
                    "provider_response_id) "
                    "VALUES (:id,:project_id,:actor_id,:run_id,:profile_id,:account_id,:provider,:model_id,"
                    ":revision,:body,:digest,:input_tokens,:output_tokens,:cost_usd,:cost_usd_ticks,:response_id)"
                ),
                {
                    "id": output_id,
                    "project_id": project_id,
                    "actor_id": actor,
                    "run_id": prepared.run_id,
                    "profile_id": prepared.profile_id,
                    "account_id": prepared.account_id,
                    "provider": prepared.provider,
                    "model_id": prepared.model_id,
                    "revision": prepared.credential_revision,
                    "body": body,
                    "digest": digest,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cost_usd": cost_usd,
                    "cost_usd_ticks": ticks,
                    "response_id": result.provider_response_id,
                },
            )
            for memory in prepared.memories:
                connection.execute(
                    text(
                        "INSERT INTO live_text_output_sources "
                        "(id,project_id,actor_id,output_id,source_project_id,memory_item_id) "
                        "VALUES (:id,:project_id,:actor_id,:output_id,:source_project_id,:memory_item_id)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": actor,
                        "output_id": output_id,
                        "source_project_id": memory["project_id"],
                        "memory_item_id": memory["id"],
                    },
                )
            connection.execute(
                text(
                    "UPDATE runs SET status='done',tokens_used=:tokens,cost_usd=:cost_usd,"
                    "finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "project_id": project_id,
                    "id": prepared.run_id,
                    "tokens": result.input_tokens + result.output_tokens,
                    "cost_usd": cost_usd,
                },
            )
            self.gateway.settle_work_order(connection, project_id, prepared.order_id, prepared.run_id, "done")
            self.gateway._event(
                connection,
                project_id,
                prepared.order_id,
                prepared.run_id,
                "live_text_finished",
                {
                    "status": "done",
                    "output_id": str(output_id),
                    "text_sha256": digest,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cost_source": "provider_reported" if ticks is not None else None,
                    "cost_usd_ticks": ticks,
                    "source_count": len(prepared.memories),
                },
                actor_id=actor,
            )
            from onefoundry.onefoundry_budget import linked_workflow, settle

            if linked_workflow(connection, project_id, prepared.order_id) is not None:
                reservation_id = connection.scalar(
                    text(
                        "SELECT id FROM onefoundry_budget_reservations WHERE project_id=:p "
                        "AND operation_key=:key"
                    ),
                    {"p": project_id, "key": f"model_attempt:{prepared.run_id}"},
                )
                if reservation_id is not None:
                    settle(
                        connection,
                        self.gateway,
                        project_id,
                        reservation_id,
                        prepared.run_id,
                        "settled",
                        result.input_tokens + result.output_tokens,
                        float(cost_usd) if cost_usd is not None else None,
                    )
            return {
                "run": self.gateway._run_row(connection, project_id, prepared.run_id),
                "output": self._output(connection, project_id, prepared.run_id),
            }

    def _terminal(
        self,
        project_id: UUID,
        prepared: Prepared,
        status: str,
        reason: str,
        diagnostic_code: str | None = None,
    ) -> None:
        with self.gateway.engine.begin() as connection:
            current = connection.scalar(
                text("SELECT status FROM runs WHERE project_id=:project_id AND id=:id FOR UPDATE"),
                {"project_id": project_id, "id": prepared.run_id},
            )
            if current != "running":
                return
            connection.execute(
                text(
                    "UPDATE runs SET status=:status,outcome_reason=:reason,finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": prepared.run_id, "status": status, "reason": reason},
            )
            self.gateway.settle_work_order(connection, project_id, prepared.order_id, prepared.run_id, status)
            payload: dict[str, Any] = {"status": status, "reason": reason, "cost_usd": None, "retry": False}
            if diagnostic_code is not None:
                payload["diagnostic_code"] = diagnostic_code
            self.gateway._event(
                connection,
                project_id,
                prepared.order_id,
                prepared.run_id,
                "live_text_finished",
                payload,
                actor_id=f"profile:{prepared.profile_id}",
            )
            from onefoundry.onefoundry_budget import linked_workflow, settle

            if linked_workflow(connection, project_id, prepared.order_id) is not None:
                reservation_id = connection.scalar(
                    text(
                        "SELECT id FROM onefoundry_budget_reservations WHERE project_id=:p "
                        "AND operation_key=:key"
                    ),
                    {"p": project_id, "key": f"model_attempt:{prepared.run_id}"},
                )
                if reservation_id is not None:
                    settle(
                        connection,
                        self.gateway,
                        project_id,
                        reservation_id,
                        prepared.run_id,
                        "unknown" if status == "unknown" else "settled",
                        None,
                        None,
                    )

    def run(
        self, project_id: UUID, order_id: UUID, *, restart: LiveTextRestart | None = None
    ) -> dict[str, Any]:
        try:
            prepared = self._prepare(project_id, order_id, restart)
        except RunPreflightBlocked as error:
            if restart is None:
                self.gateway.block_unavailable_capability(project_id, order_id, str(error))
            raise
        try:
            request = TextRequest(
                model=prepared.model_id,
                instructions=prepared.instructions,
                prompt=prepared.prompt,
                max_output_tokens=prepared.max_output_tokens or self.settings.live_text_max_output_tokens,
                timeout_seconds=prepared.timeout_seconds or self.settings.live_text_timeout_seconds,
                request_id=str(prepared.run_id),
                reasoning_effort=prepared.reasoning_effort,
                images=prepared.images,
            )
            if prepared.tools_active:
                from onefoundry.live_tool_sdk import run_live_tool_sdk

                result = run_live_tool_sdk(self.gateway, project_id, prepared, self.settings)
            else:
                result = self.transport_factory(prepared, self.settings).generate(request)
        except (AuthenticationError, RateLimitError, InvalidRequestError, CodexStreamFailure) as error:
            try:
                self._terminal(project_id, prepared, "failed", "provider_rejected", _diagnostic_code(error))
            except SQLAlchemyError:
                raise LiveTextUnknown(prepared.run_id) from None
            raise LiveTextFailure(prepared.run_id) from None
        except Exception as error:
            from onefoundry.role_tool_admission import (
                ToolDenied,
                ToolInputError,
                ToolOutputRejected,
                ToolPolicyRejected,
            )

            if isinstance(error, (ToolDenied, ToolInputError, ToolOutputRejected, ToolPolicyRejected)):
                try:
                    self._terminal(project_id, prepared, "failed", "tool_rejected")
                except SQLAlchemyError:
                    raise LiveTextUnknown(prepared.run_id) from None
                raise LiveTextFailure(prepared.run_id) from None
            try:
                self._terminal(
                    project_id,
                    prepared,
                    "unknown",
                    "provider_outcome_unknown",
                    _diagnostic_code(error),
                )
            except SQLAlchemyError:
                pass
            raise LiveTextUnknown(prepared.run_id) from None
        try:
            return self._complete(project_id, prepared, result)
        except Exception:
            try:
                with self.gateway.engine.connect() as connection:
                    output = self._output(connection, project_id, prepared.run_id)
                    run = self.gateway._run_row(connection, project_id, prepared.run_id)
                    if output is not None and run["status"] == "done":
                        return {"run": run, "output": output}
                self._terminal(project_id, prepared, "unknown", "persistence_outcome_unknown")
            except SQLAlchemyError:
                pass
            raise LiveTextUnknown(prepared.run_id) from None

    def reconcile_interrupted(self) -> None:
        with self.gateway.engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT r.project_id,r.id,r.work_order_id,r.profile_id FROM runs r "
                        "WHERE r.mode='live_text' AND r.status='running' "
                        "AND NOT EXISTS(SELECT 1 FROM task_session_attempts a "
                        "WHERE a.project_id=r.project_id AND a.id=r.id) FOR UPDATE"
                    ),
                )
                .mappings()
                .all()
            )
            for row in rows:
                connection.execute(
                    text(
                        "UPDATE runs SET status='unknown',outcome_reason='process_interrupted',"
                        "finished_at=clock_timestamp() WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": row["project_id"], "id": row["id"]},
                )
                self.gateway.settle_work_order(
                    connection, row["project_id"], row["work_order_id"], row["id"], "unknown"
                )
                self.gateway._event(
                    connection,
                    row["project_id"],
                    row["work_order_id"],
                    row["id"],
                    "live_text_finished",
                    {"status": "unknown", "reason": "process_interrupted", "cost_usd": None, "retry": False},
                    actor_id=f"profile:{row['profile_id']}",
                )
                from onefoundry.onefoundry_budget import linked_workflow, settle

                if linked_workflow(connection, row["project_id"], row["work_order_id"]) is not None:
                    reservation_id = connection.scalar(
                        text(
                            "SELECT id FROM onefoundry_budget_reservations WHERE project_id=:p "
                            "AND operation_key=:key AND state='reserved'"
                        ),
                        {"p": row["project_id"], "key": f"model_attempt:{row['id']}"},
                    )
                    if reservation_id is not None:
                        settle(
                            connection,
                            self.gateway,
                            row["project_id"],
                            reservation_id,
                            row["id"],
                            "unknown",
                            None,
                            None,
                        )
