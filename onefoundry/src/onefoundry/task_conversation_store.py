"""One explicit provider turn per operator message; only bounded public updates enter events."""

import hashlib
import logging
import re
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry import memory_store, task_automation
from onefoundry.inference import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
    TextRequest,
    TextResult,
)
from onefoundry.inference.subscription_codex import CodexStreamFailure
from onefoundry.input_file_context import select_context, verified_images
from onefoundry.input_file_turn_readback import turn_input_readback
from onefoundry.job_dossier import build as build_job_dossier
from onefoundry.live_text_store import Prepared, TextTransport, _diagnostic_code, _provider_transport
from onefoundry.memory_policy import work_memory_query
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.profile_hierarchy import lock_project
from onefoundry.profile_text_runtime import resolve as resolve_profile_text
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.settings import Settings
from onefoundry.skill_store import select_procedures
from onefoundry.task_conversation_intent import DISCUSSION_INSTRUCTIONS, is_discussion_turn
from onefoundry.task_conversation_schemas import NewConversation, TurnSend
from onefoundry.tool_gateway import Conflict, NotFound
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TurnSource = Literal["operator_ui", "discord", "wechat", "system_triage", "system_followup"]
_LOG = logging.getLogger(__name__)


class ConversationFailed(Exception):
    def __init__(self, turn_id: UUID) -> None:
        self.turn_id = turn_id


class ConversationUnknown(Exception):
    def __init__(self, turn_id: UUID) -> None:
        self.turn_id = turn_id


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unknown_acknowledged(
    connection: Any, project_id: UUID, order_id: UUID, conversation_id: UUID, turn_id: UUID
) -> bool:
    return bool(
        connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM events WHERE project_id=:project_id "
                "AND work_order_id=:order_id "
                "AND event_type='task_conversation_unknown_acknowledged' "
                "AND payload->>'conversation_id'=:conversation_id "
                "AND payload->>'turn_id'=:turn_id)"
            ),
            {
                "project_id": project_id,
                "order_id": order_id,
                "conversation_id": str(conversation_id),
                "turn_id": str(turn_id),
            },
        )
    )


def _turn_skill_selections(
    connection: Any, project_id: UUID, order_id: UUID, turn_ids: list[str]
) -> dict[str, list[Any]]:
    rows = connection.execute(
        text(
            "SELECT payload FROM events WHERE project_id=:project_id AND work_order_id=:order_id "
            "AND event_type='task_conversation_turn_started' "
            "AND payload->>'turn_id'=ANY(:turn_ids) ORDER BY created_at,id"
        ),
        {"project_id": project_id, "order_id": order_id, "turn_ids": turn_ids},
    ).mappings()
    selections: dict[str, list[Any]] = {}
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, dict) and isinstance(payload.get("turn_id"), str):
            skills = payload.get("skills", [])
            selections[payload["turn_id"]] = skills if isinstance(skills, list) else []
    return selections


def _turn_diagnostics(
    connection: Any, project_id: UUID, order_id: UUID, turn_ids: list[str]
) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT payload->>'turn_id' AS turn_id,payload->>'diagnostic_code' AS code "
            "FROM events WHERE project_id=:project_id AND work_order_id=:order_id "
            "AND event_type='task_conversation_turn_finished' AND payload ? 'diagnostic_code' "
            "AND payload->>'turn_id'=ANY(:turn_ids) ORDER BY created_at,id"
        ),
        {"project_id": project_id, "order_id": order_id, "turn_ids": turn_ids},
    ).mappings()
    return {
        row["turn_id"]: row["code"]
        for row in rows
        if isinstance(row["turn_id"], str)
        and isinstance(row["code"], str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", row["code"])
    }


def _turn_progress(
    connection: Any, project_id: UUID, order_id: UUID, turn_ids: list[str]
) -> dict[str, list[str]]:
    rows = connection.execute(
        text(
            "SELECT payload->>'turn_id' AS turn_id,payload->>'text' AS summary "
            "FROM events WHERE project_id=:project_id AND work_order_id=:order_id "
            "AND event_type='task_conversation_progress' "
            "AND payload->>'turn_id'=ANY(:turn_ids) ORDER BY created_at,id"
        ),
        {"project_id": project_id, "order_id": order_id, "turn_ids": turn_ids},
    ).mappings()
    updates: dict[str, list[str]] = {}
    for row in rows:
        turn_id, summary = row["turn_id"], row["summary"]
        if isinstance(turn_id, str) and isinstance(summary, str) and summary.strip():
            items = updates.setdefault(turn_id, [])
            if len(items) < 8:
                items.append(summary)
    return updates


def _turn_automation(connection: Any, project_id: UUID, turn_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT decision,status,rule_id,rule_version,project_version,plan_version,"
                "assigned_work_order_id,error_code "
                "FROM task_automation_decisions WHERE project_id=:p AND turn_id=:turn"
            ),
            {"p": project_id, "turn": turn_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    receipt = dict(row)
    receipt["validation_errors"] = (
        connection.scalar(
            text(
                "SELECT payload->'validation_errors' FROM events WHERE project_id=:p "
                "AND event_type='task_automation_decision' AND payload->>'turn_id'=:turn "
                "ORDER BY created_at DESC,id DESC LIMIT 1"
            ),
            {"p": project_id, "turn": str(turn_id)},
        )
        or []
    )
    receipt["assignments"] = [
        dict(item)
        for item in connection.execute(
            text(
                "SELECT o.id,o.title,o.status,p.name AS profile_name "
                "FROM task_assignment_links l "
                "JOIN task_automation_decisions d ON d.project_id=l.project_id "
                "AND d.id=l.decision_id "
                "JOIN work_orders o ON o.project_id=l.project_id AND o.id=l.work_order_id "
                "JOIN profiles p ON p.project_id=o.project_id AND p.id=o.profile_id "
                "WHERE l.project_id=:p AND d.turn_id=:turn ORDER BY l.position"
            ),
            {"p": project_id, "turn": turn_id},
        ).mappings()
    ]
    return receipt


class TaskConversationStore:
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

    def _order(self, connection: Any, project_id: UUID, order_id: UUID, *, lock: bool) -> Any:
        statement = (
            "SELECT id,title,instructions,profile_id,status FROM work_orders "
            "WHERE project_id=:project_id AND id=:id FOR UPDATE"
            if lock
            else "SELECT id,title,instructions,profile_id,status FROM work_orders "
            "WHERE project_id=:project_id AND id=:id"
        )
        row = (
            connection.execute(
                text(statement),
                {"project_id": project_id, "id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("work order not found")
        return row

    def _conversation(self, connection: Any, project_id: UUID, order_id: UUID, profile_id: UUID) -> Any:
        statement = (
            "SELECT id,project_id,work_order_id,profile_id,title,is_active FROM task_conversations "
            "WHERE project_id=:project_id AND work_order_id=:order_id AND profile_id=:profile_id "
            "AND is_active"
        )
        row = (
            connection.execute(
                text(statement),
                {"project_id": project_id, "order_id": order_id, "profile_id": profile_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("conversation not opened")
        return row

    def _read(
        self,
        connection: Any,
        project_id: UUID,
        order_id: UUID,
        profile_id: UUID | None = None,
        conversation_id: UUID | None = None,
        before_sequence: int | None = None,
    ) -> dict[str, Any]:
        if conversation_id is not None:
            conversation = (
                connection.execute(
                    text(
                        "SELECT id,project_id,work_order_id,profile_id,title,is_active "
                        "FROM task_conversations WHERE project_id=:project_id "
                        "AND work_order_id=:order_id AND id=:conversation_id"
                    ),
                    {"project_id": project_id, "order_id": order_id, "conversation_id": conversation_id},
                )
                .mappings()
                .one_or_none()
            )
            if conversation is None:
                raise NotFound("conversation not found")
        else:
            if profile_id is None:
                order = self._order(connection, project_id, order_id, lock=False)
                profile_id = order["profile_id"]
            conversation = self._conversation(connection, project_id, order_id, profile_id)
        turns = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,client_turn_id,sequence,status,operator_text,assistant_text,assistant_sha256,"
                    "assistant_actor_id,reason,provider,model_id,input_tokens,output_tokens,cost_usd,"
                    "created_at,finished_at,input_sections_omitted,source_kind FROM task_conversation_turns "
                    "WHERE project_id=:project_id AND conversation_id=:conversation_id "
                    "AND (CAST(:before AS bigint) IS NULL OR sequence<:before) "
                    "ORDER BY sequence DESC LIMIT :limit"
                ),
                {
                    "project_id": project_id,
                    "conversation_id": conversation["id"],
                    "limit": self.settings.task_conversation_max_turns,
                    "before": before_sequence,
                },
            ).mappings()
        ]
        turns.sort(key=lambda turn: turn["sequence"])
        turn_ids = [str(turn["id"]) for turn in turns]
        skill_selections = _turn_skill_selections(connection, project_id, order_id, turn_ids)
        diagnostics = _turn_diagnostics(connection, project_id, order_id, turn_ids)
        progress = _turn_progress(connection, project_id, order_id, turn_ids)
        for turn in turns:
            turn["selected_skills"] = skill_selections.get(str(turn["id"]), [])
            turn["diagnostic_code"] = diagnostics.get(str(turn["id"]))
            turn["progress_updates"] = progress.get(str(turn["id"]), [])
            turn.update(turn_input_readback(connection, project_id, turn["id"]))
            turn["automation_decision"] = _turn_automation(connection, project_id, turn["id"])
            turn["discussion_only"] = is_discussion_turn(connection, project_id, turn["id"])
        last = (
            connection.execute(
                text(
                    "SELECT id,sequence,status FROM task_conversation_turns WHERE project_id=:p "
                    "AND conversation_id=:c ORDER BY sequence DESC LIMIT 1"
                ),
                {"p": project_id, "c": conversation["id"]},
            )
            .mappings()
            .one_or_none()
        )
        latest = last["status"] if last else None
        acknowledged = latest == "unknown" and _unknown_acknowledged(
            connection, project_id, order_id, conversation["id"], last["id"]
        )
        status = (
            "busy"
            if latest == "running"
            else "unknown"
            if latest == "unknown" and not acknowledged
            else "open"
        )
        return {
            "id": conversation["id"],
            "project_id": project_id,
            "work_order_id": order_id,
            "profile_id": conversation["profile_id"],
            "title": conversation["title"],
            "is_active": conversation["is_active"],
            "status": status,
            "turns": turns,
            "turns_omitted": max(0, (turns[0]["sequence"] - 1) if turns else 0),
            "latest_sequence": last["sequence"] if last else 0,
        }

    def get(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._read(connection, project_id, order_id)

    def lookup_turn(self, project_id: UUID, order_id: UUID, client_turn_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            self._order(connection, project_id, order_id, lock=False)
            row = (
                connection.execute(
                    text(
                        "SELECT c.id AS conversation_id,c.profile_id,t.id,t.client_turn_id,t.sequence,"
                        "t.status,t.operator_text,t.assistant_text,t.assistant_sha256,"
                        "t.assistant_actor_id,t.reason,t.provider,t.model_id,t.input_tokens,"
                        "t.output_tokens,t.cost_usd,t.created_at,t.finished_at,t.input_sections_omitted,"
                        "t.source_kind "
                        "FROM task_conversation_turns t JOIN task_conversations c "
                        "ON c.project_id=t.project_id AND c.id=t.conversation_id "
                        "WHERE t.project_id=:project_id AND c.work_order_id=:order_id "
                        "AND t.client_turn_id=:client_turn_id"
                    ),
                    {"project_id": project_id, "order_id": order_id, "client_turn_id": client_turn_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("conversation turn not found")
            saved = dict(row)
            saved["selected_skills"] = _turn_skill_selections(
                connection, project_id, order_id, [str(saved["id"])]
            ).get(str(saved["id"]), [])
            saved["diagnostic_code"] = _turn_diagnostics(
                connection, project_id, order_id, [str(saved["id"])]
            ).get(str(saved["id"]))
            saved.update(turn_input_readback(connection, project_id, saved["id"]))
            saved["automation_decision"] = _turn_automation(connection, project_id, saved["id"])
            saved["discussion_only"] = is_discussion_turn(connection, project_id, saved["id"])
            return {
                "conversation_id": saved.pop("conversation_id"),
                "profile_id": saved.pop("profile_id"),
                "turn": saved,
            }

    def _get_profile(self, project_id: UUID, order_id: UUID, profile_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._read(connection, project_id, order_id, profile_id)

    def list_for_order(self, project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self._order(connection, project_id, order_id, lock=False)
            rows = (
                connection.execute(
                    text(
                        "SELECT c.id,c.project_id,c.work_order_id,c.profile_id,c.title,c.is_active,"
                        "c.created_at,"
                        "t.id AS latest_turn_id,t.status AS latest_status FROM task_conversations c "
                        "LEFT JOIN LATERAL (SELECT id,status FROM task_conversation_turns "
                        "WHERE project_id=c.project_id AND conversation_id=c.id "
                        "ORDER BY sequence DESC LIMIT 1) t ON true "
                        "WHERE c.project_id=:project_id AND c.work_order_id=:order_id "
                        "ORDER BY c.created_at,c.id"
                    ),
                    {"project_id": project_id, "order_id": order_id},
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                saved = dict(row)
                latest = saved.pop("latest_status")
                latest_turn_id = saved.pop("latest_turn_id")
                acknowledged = latest == "unknown" and _unknown_acknowledged(
                    connection, project_id, order_id, saved["id"], latest_turn_id
                )
                saved["status"] = (
                    "busy"
                    if latest == "running"
                    else "unknown"
                    if latest == "unknown" and not acknowledged
                    else "open"
                )
                result.append(saved)
            return result

    def get_by_id(
        self, project_id: UUID, order_id: UUID, conversation_id: UUID, before_sequence: int | None = None
    ) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            self._order(connection, project_id, order_id, lock=False)
            return self._read(
                connection,
                project_id,
                order_id,
                conversation_id=conversation_id,
                before_sequence=before_sequence,
            )

    def open(self, project_id: UUID, order_id: UUID) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            order = self._order(connection, project_id, order_id, lock=True)
            existing = connection.execute(
                text(
                    "SELECT id FROM task_conversations WHERE project_id=:project_id "
                    "AND work_order_id=:order_id AND profile_id=:profile_id AND is_active"
                ),
                {"project_id": project_id, "order_id": order_id, "profile_id": order["profile_id"]},
            ).scalar_one_or_none()
            created = existing is None
            if created:
                conversation_id = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO task_conversations "
                        "(id,project_id,actor_id,work_order_id,profile_id,title,is_active) "
                        "VALUES (:id,:project_id,:actor_id,:order_id,:profile_id,'Main chat',true)"
                    ),
                    {
                        "id": conversation_id,
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "order_id": order_id,
                        "profile_id": order["profile_id"],
                    },
                )
                self.gateway._event(
                    connection,
                    project_id,
                    order_id,
                    None,
                    "task_conversation_opened",
                    {"conversation_id": str(conversation_id), "profile_id": str(order["profile_id"])},
                )
            return self._read(connection, project_id, order_id), created

    def new(self, project_id: UUID, order_id: UUID, data: NewConversation) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            order = self._order(connection, project_id, order_id, lock=True)
            existing = (
                connection.execute(
                    text(
                        "SELECT work_order_id,profile_id,title FROM task_conversations "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": data.id},
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["work_order_id"] != order_id
                    or existing["profile_id"] != order["profile_id"]
                    or existing["title"] != data.title
                ):
                    raise Conflict("chat creation ID was already used for another request")
                return self._read(connection, project_id, order_id, conversation_id=data.id), False
            try:
                active = self._conversation(connection, project_id, order_id, order["profile_id"])
            except NotFound:
                active = None
            if active is not None:
                last = (
                    connection.execute(
                        text(
                            "SELECT id,status FROM task_conversation_turns "
                            "WHERE project_id=:project_id AND conversation_id=:conversation_id "
                            "ORDER BY sequence DESC LIMIT 1"
                        ),
                        {"project_id": project_id, "conversation_id": active["id"]},
                    )
                    .mappings()
                    .one_or_none()
                )
                if last is not None and (
                    last["status"] == "running"
                    or last["status"] == "unknown"
                    and not _unknown_acknowledged(connection, project_id, order_id, active["id"], last["id"])
                ):
                    raise Conflict("check the current agent reply before opening another chat")
            connection.execute(
                text(
                    "UPDATE task_conversations SET is_active=false WHERE project_id=:project_id "
                    "AND work_order_id=:order_id AND profile_id=:profile_id AND is_active"
                ),
                {"project_id": project_id, "order_id": order_id, "profile_id": order["profile_id"]},
            )
            connection.execute(
                text(
                    "INSERT INTO task_conversations "
                    "(id,project_id,actor_id,work_order_id,profile_id,title,is_active) "
                    "VALUES (:id,:project_id,:actor_id,:order_id,:profile_id,:title,true)"
                ),
                {
                    "id": data.id,
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "order_id": order_id,
                    "profile_id": order["profile_id"],
                    "title": data.title,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "task_conversation_opened",
                {"conversation_id": str(data.id), "profile_id": str(order["profile_id"])},
            )
            return self._read(connection, project_id, order_id, conversation_id=data.id), True

    def acknowledge_unknown(self, project_id: UUID, order_id: UUID, turn_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            order = self._order(connection, project_id, order_id, lock=True)
            conversation = self._conversation(connection, project_id, order_id, order["profile_id"])
            if _unknown_acknowledged(connection, project_id, order_id, conversation["id"], turn_id):
                return self._read(connection, project_id, order_id)
            latest = (
                connection.execute(
                    text(
                        "SELECT id,sequence,status FROM task_conversation_turns "
                        "WHERE project_id=:project_id AND conversation_id=:conversation_id "
                        "ORDER BY sequence DESC LIMIT 1"
                    ),
                    {"project_id": project_id, "conversation_id": conversation["id"]},
                )
                .mappings()
                .one_or_none()
            )
            if latest is None or latest["id"] != turn_id or latest["status"] != "unknown":
                raise Conflict("latest conversation turn does not have an unknown outcome")
            self.gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "task_conversation_unknown_acknowledged",
                {
                    "conversation_id": str(conversation["id"]),
                    "turn_id": str(turn_id),
                    "sequence": latest["sequence"],
                },
            )
            return self._read(connection, project_id, order_id)

    def _binding(
        self,
        connection: Any,
        project_id: UUID,
        order: Any,
        turn_id: UUID,
        operator_text: str,
        total_done: int,
        source_kind: TurnSource,
        discussion_only: bool = False,
    ) -> tuple[Prepared, list[dict[str, Any]]]:
        profile = resolve_profile_text(connection, project_id, order["profile_id"], self.secrets)
        memory_query = work_memory_query(str(order["title"]), str(order["instructions"]))
        scope = "project" if profile.memory_scope == "project" else None
        memories = memory_store.retrieve_memory(connection, project_id, memory_query, 5, scope, self.settings)
        memory_text = "\n".join(f"- {item['id']}: {item['text']}" for item in memories) or "none"
        job_state = build_job_dossier(connection, project_id, order["id"])
        instructions = (
            local_text(profile.instructions)
            + "\n\nIn your visible reply, briefly state your plan, what you found or verified, "
            "and the next step when relevant. State uncertainty plainly. Do not reveal private "
            "reasoning or claim tool, shell, or coding actions unless they actually ran."
        )
        if source_kind == "system_followup":
            instructions += (
                "\n\nThis is an automatic saved child outcome, not a new operator instruction. "
                "Review the current parent dossier, child evidence, and project plan. Continue "
                "the operator's goal with a bounded assignment if needed. Report blockers or "
                "request a decision when necessary. Do not claim parent completion without evidence."
            )
        automation_context = ""
        if discussion_only:
            instructions += "\n\n" + DISCUSSION_INSTRUCTIONS
        elif source_kind != "system_triage":
            instructions += "\n\n" + task_automation.DECISION_INSTRUCTIONS
            automation_context = "\n\n" + task_automation.decision_context(
                connection, project_id, profile.profile_id, self.settings
            )
        base_context = (
            f"Work order: {order['title']}\nInstructions: {order['instructions']}\n"
            f"Selected procedures (task guidance only; no tools or permissions):\nnone\n"
            f"Approved visible memory:\n{memory_text}\n\n{job_state}{automation_context}"
        )
        base_bytes = (
            len(instructions.encode("utf-8"))
            + len(base_context.encode("utf-8"))
            + len(operator_text.encode("utf-8"))
        )
        available_content_bytes = max(
            0,
            min(
                self.settings.skill_auto_max_content_bytes,
                self.settings.live_text_max_prompt_bytes
                - base_bytes
                - (256 if source_kind != "system_triage" else 2048),
            ),
        )
        procedures = select_procedures(
            connection,
            project_id,
            profile.profile_id,
            str(order["title"]),
            f"{order['instructions']}\n{operator_text}",
            max_auto=self.settings.skill_auto_max_count,
            max_content_bytes=available_content_bytes,
        )
        while True:
            skills_text = (
                "\n\n".join(
                    f"SKILL.md {item['skill_name']} v{item['version']} "
                    f"sha256:{item['sha256']}\n{item['content']}"
                    for item in procedures
                )
                or "none"
            )
            context = local_text(
                f"Work order: {order['title']}\nInstructions: {order['instructions']}\n"
                f"Selected procedures (task guidance only; no tools or permissions):\n{skills_text}\n"
                f"Approved visible memory:\n{memory_text}\n\n{job_state}{automation_context}"
            )
            try:
                self._prompt(context, [], operator_text, instructions, total_done, source_kind)
                break
            except Conflict:
                if not procedures or procedures[-1]["selection_source"] != "automatic":
                    raise
                procedures.pop()
        return Prepared(
            turn_id,
            order["id"],
            profile.profile_id,
            profile.account_id,
            profile.provider,
            profile.model_id,
            profile.credential_revision,
            profile.endpoint,
            profile.api_key,
            instructions,
            context,
            memories,
            profile.codex_state,
            profile.grok_state,
        ), procedures

    def _prompt(
        self,
        base: str,
        prior: list[Any],
        current: str,
        instructions: str,
        total_done: int,
        source_kind: TurnSource = "operator_ui",
    ) -> str:
        history = list(prior)
        while True:
            omitted = total_done - len(history)
            lines = [
                base,
                "Conversation with the assigned agent. Respond to the latest project update "
                "using saved job facts."
                if source_kind == "system_followup"
                else "Conversation with the assigned agent. Respond to the latest operator message only.",
                f"Earlier confirmed turns omitted: {omitted}.",
            ]
            for item in history:
                label = "Project update" if item["source_kind"] == "system_followup" else "Operator"
                lines.extend((f"{label}: {item['operator_text']}", f"Agent: {item['assistant_text']}"))
            label = "Project update" if source_kind == "system_followup" else "Operator"
            lines.append(f"{label}: {current}")
            prompt = "\n\n".join(lines)
            if len(instructions.encode()) + len(prompt.encode()) <= self.settings.live_text_max_prompt_bytes:
                return prompt
            if not history:
                raise Conflict("conversation prompt exceeds configured limit")
            history = history[1:]

    def _prepare(
        self,
        project_id: UUID,
        order_id: UUID,
        data: TurnSend,
        source_kind: TurnSource = "operator_ui",
        remote_inbound_id: UUID | None = None,
    ) -> tuple[Prepared | None, dict[str, Any]]:
        if len(data.text.encode("utf-8")) > self.settings.live_text_max_prompt_bytes:
            raise Conflict("operator message exceeds configured prompt limit")
        if (source_kind in {"discord", "wechat"}) != (remote_inbound_id is not None):
            raise Conflict("conversation source binding is invalid")
        if data.discussion_only and source_kind != "operator_ui":
            raise Conflict("discussion-only intent is restricted to the operator chat")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            if remote_inbound_id is not None:
                matched = connection.scalar(
                    text(
                        "SELECT 1 FROM remote_inbound i JOIN remote_channels c "
                        "ON c.project_id=i.project_id AND c.id=i.channel_id "
                        "WHERE i.project_id=:p AND i.id=:id AND c.platform=:platform "
                        "AND i.client_turn_id=:turn AND i.work_order_id=:work "
                        "AND i.body_sha256=:digest AND i.status='processing' "
                        "AND c.enabled AND c.identity_confirmed "
                        "AND i.sender_id=c.sender_id AND i.chat_id=c.chat_id"
                    ),
                    {
                        "p": project_id,
                        "id": remote_inbound_id,
                        "platform": source_kind,
                        "turn": data.client_turn_id,
                        "work": order_id,
                        "digest": _digest(data.text),
                    },
                )
                if matched is None:
                    raise Conflict("conversation remote source does not match its confirmed inbound")
            order = self._order(connection, project_id, order_id, lock=True)
            existing = (
                connection.execute(
                    text(
                        "SELECT t.id,t.sequence,t.operator_sha256,t.conversation_id,"
                        "t.source_kind,t.remote_inbound_id,"
                        "c.work_order_id,c.profile_id FROM task_conversation_turns t "
                        "JOIN task_conversations c ON c.project_id=t.project_id "
                        "AND c.id=t.conversation_id "
                        "WHERE t.project_id=:project_id AND t.client_turn_id=:client_turn_id"
                    ),
                    {"project_id": project_id, "client_turn_id": data.client_turn_id},
                )
                .mappings()
                .one_or_none()
            )
            digest = _digest(data.text)
            if existing is not None:
                existing_ids: set[UUID] = set(
                    connection.execute(
                        text(
                            "SELECT file_id FROM input_file_links WHERE project_id=:project_id "
                            "AND turn_id=:turn_id AND scope='turn'"
                        ),
                        {"project_id": project_id, "turn_id": existing["id"]},
                    ).scalars()
                )
                if (
                    existing["work_order_id"] != order_id
                    or existing["profile_id"] != order["profile_id"]
                    or existing["conversation_id"] != data.conversation_id
                    or existing["operator_sha256"] != digest
                    or existing["source_kind"] != source_kind
                    or existing["remote_inbound_id"] != remote_inbound_id
                    or existing_ids != set(data.attachment_ids)
                    or len(data.attachment_ids) != len(existing_ids)
                    or data.expected_sequence != existing["sequence"] - 1
                    or is_discussion_turn(connection, project_id, existing["id"]) != data.discussion_only
                ):
                    raise Conflict("client turn ID was already used for a different request")
                saved = self._read(
                    connection, project_id, order_id, conversation_id=existing["conversation_id"]
                )
                return None, {
                    "conversation": saved,
                    "turn": self.lookup_turn(project_id, order_id, data.client_turn_id)["turn"],
                }
            team_mode = connection.scalar(
                text(
                    "SELECT 1 FROM task_sessions WHERE project_id=:project_id "
                    "AND work_order_id=:order_id AND execution_mode='team' "
                    "AND kind<>'consultation'"
                ),
                {"project_id": project_id, "order_id": order_id},
            )
            if team_mode is not None:
                raise Conflict("this work order uses the team session; refresh the task")
            try:
                conversation = self._conversation(connection, project_id, order_id, order["profile_id"])
            except NotFound:
                old = connection.scalar(
                    text(
                        "SELECT 1 FROM task_conversations WHERE project_id=:project_id "
                        "AND work_order_id=:order_id LIMIT 1"
                    ),
                    {"project_id": project_id, "order_id": order_id},
                )
                if old:
                    raise Conflict("work order agent changed; open this agent's conversation") from None
                raise
            if conversation["id"] != data.conversation_id:
                raise Conflict("conversation changed; refresh the assigned agent before sending")
            if order["profile_id"] != conversation["profile_id"]:
                raise Conflict("work order was assigned to another agent; open a new task for that agent")
            last = (
                connection.execute(
                    text(
                        "SELECT id,sequence,status FROM task_conversation_turns "
                        "WHERE project_id=:project_id AND conversation_id=:conversation_id "
                        "ORDER BY sequence DESC LIMIT 1"
                    ),
                    {"project_id": project_id, "conversation_id": conversation["id"]},
                )
                .mappings()
                .one_or_none()
            )
            if last and last["status"] == "running":
                raise Conflict("agent response is still pending; refresh the conversation")
            if (
                last
                and last["status"] == "unknown"
                and not _unknown_acknowledged(
                    connection, project_id, order_id, conversation["id"], last["id"]
                )
            ):
                raise Conflict("prior agent response has an unknown outcome; inspect it before further sends")
            sequence = last["sequence"] if last else 0
            if data.expected_sequence != sequence:
                raise Conflict("conversation changed; refresh before sending")
            total_done = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM task_conversation_turns WHERE project_id=:project_id "
                        "AND conversation_id=:conversation_id AND status='done'"
                    ),
                    {"project_id": project_id, "conversation_id": conversation["id"]},
                )
                or 0
            )
            history = (
                connection.execute(
                    text(
                        "SELECT sequence,operator_text,assistant_text,source_kind FROM ("
                        "SELECT sequence,operator_text,assistant_text,source_kind,"
                        "sum(octet_length(operator_text)+octet_length(assistant_text)+32) "
                        "OVER (ORDER BY sequence DESC) AS used_bytes "
                        "FROM (SELECT sequence,operator_text,assistant_text,source_kind "
                        "FROM task_conversation_turns WHERE project_id=:project_id "
                        "AND conversation_id=:conversation_id AND status='done' "
                        "ORDER BY sequence DESC LIMIT :history_turns) recent) ranked "
                        "WHERE used_bytes <= :history_bytes ORDER BY sequence"
                    ),
                    {
                        "project_id": project_id,
                        "conversation_id": conversation["id"],
                        "history_bytes": self.settings.live_text_max_prompt_bytes,
                        "history_turns": self.settings.task_conversation_max_turns,
                    },
                )
                .mappings()
                .all()
            )
            turn_id = uuid4()
            prepared, procedures = self._binding(
                connection,
                project_id,
                order,
                turn_id,
                data.text,
                total_done,
                source_kind,
                discussion_only=data.discussion_only,
            )
            file_budget = min(
                12000,
                self.settings.live_text_max_prompt_bytes
                - len(prepared.instructions.encode("utf-8"))
                - len(prepared.prompt.encode("utf-8"))
                - len(data.text.encode("utf-8"))
                - 1024,
            )
            selected_files = select_context(
                connection,
                project_id,
                order_id,
                data.attachment_ids,
                f"{order['instructions']} {data.text}",
                file_budget,
                self.settings.input_file_max_link_count,
                allow_visual=prepared.provider in {"openai", "openai_codex_runtime"},
                max_visual_bytes=self.settings.input_file_max_visual_bytes,
                max_visual_count=self.settings.input_file_max_visual_count,
            )
            images = verified_images(self.settings.input_file_root, project_id, selected_files.visuals)
            prompt_base = prepared.prompt + ("\n\n" + selected_files.text if selected_files.text else "")
            prompt = self._prompt(
                prompt_base,
                list(history),
                data.text,
                prepared.instructions,
                total_done,
                source_kind,
            )
            prepared = Prepared(
                turn_id,
                prepared.order_id,
                prepared.profile_id,
                prepared.account_id,
                prepared.provider,
                prepared.model_id,
                prepared.credential_revision,
                prepared.endpoint,
                prepared.api_key,
                prepared.instructions,
                prompt,
                prepared.memories,
                prepared.codex_state,
                prepared.grok_state,
                images=images,
            )
            connection.execute(
                text(
                    "INSERT INTO task_conversation_turns "
                    "(id,project_id,actor_id,conversation_id,client_turn_id,sequence,status,operator_text,"
                    "operator_sha256,prompt_sha256,profile_id,account_id,provider,model_id,"
                    "credential_revision,input_sections_omitted,source_kind,remote_inbound_id) VALUES "
                    "(:id,:project_id,:actor_id,:conversation_id,:client_turn_id,:sequence,'running',"
                    ":operator_text,:operator_sha256,:prompt_sha256,:profile_id,:account_id,:provider,"
                    ":model_id,:credential_revision,:input_sections_omitted,:source_kind,:remote_inbound_id)"
                ),
                {
                    "id": turn_id,
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "conversation_id": conversation["id"],
                    "client_turn_id": data.client_turn_id,
                    "sequence": sequence + 1,
                    "operator_text": data.text,
                    "operator_sha256": digest,
                    "prompt_sha256": _digest(prompt),
                    "profile_id": prepared.profile_id,
                    "account_id": prepared.account_id,
                    "provider": prepared.provider,
                    "model_id": prepared.model_id,
                    "credential_revision": prepared.credential_revision,
                    "input_sections_omitted": selected_files.omitted_chunks,
                    "source_kind": source_kind,
                    "remote_inbound_id": remote_inbound_id,
                },
            )
            for file_id in selected_files.attachment_ids:
                connection.execute(
                    text(
                        "INSERT INTO input_file_links "
                        "(id,project_id,actor_id,file_id,scope,work_order_id,turn_id) VALUES "
                        "(:id,:project_id,:actor_id,:file_id,'turn',:order_id,:turn_id)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "file_id": file_id,
                        "order_id": order_id,
                        "turn_id": turn_id,
                    },
                )
            for use in selected_files.uses:
                connection.execute(
                    text(
                        "INSERT INTO input_file_context_uses "
                        "(id,project_id,actor_id,turn_id,file_id,chunk_id,kind,"
                        "source_label,sha256,byte_count) VALUES "
                        "(:id,:project_id,:actor_id,:turn_id,:file_id,:chunk_id,'text',:label,:sha,:size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "turn_id": turn_id,
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
                        "(id,project_id,actor_id,turn_id,file_id,kind,source_label,sha256,byte_count) "
                        "VALUES (:id,:project_id,:actor_id,:turn_id,:file_id,'visual',:label,:sha,:size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "turn_id": turn_id,
                        "file_id": visual.file_id,
                        "label": visual.file_name,
                        "sha": visual.file_sha256,
                        "size": visual.byte_size,
                    },
                )
            self.gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "task_conversation_turn_started",
                {
                    "conversation_id": str(conversation["id"]),
                    "turn_id": str(turn_id),
                    "sequence": sequence + 1,
                    "profile_id": str(prepared.profile_id),
                    "operator_sha256": digest,
                    "prompt_sha256": _digest(prompt),
                    "provider": prepared.provider,
                    "model_id": prepared.model_id,
                    "skills": [
                        {
                            "skill_id": str(item["skill_id"]),
                            "skill_name": item["skill_name"],
                            "version": item["version"],
                            "sha256": item["sha256"],
                            "selection_source": item["selection_source"],
                        }
                        for item in procedures
                    ],
                    "tools_active": False,
                    "discussion_only": data.discussion_only,
                    "source_kind": source_kind,
                    "attachment_ids": [str(file_id) for file_id in selected_files.attachment_ids],
                    "input_sections_omitted": selected_files.omitted_chunks,
                    "cost_usd": None,
                },
            )
            return prepared, {}

    def _finish(
        self,
        project_id: UUID,
        prepared: Prepared,
        status: str,
        result: TextResult | None,
        reason: str | None,
        diagnostic_code: str | None = None,
    ) -> None:
        body = local_text(result.text) if result is not None else None
        if body is not None and len(body.encode()) > self.settings.live_text_max_output_bytes:
            raise ValueError("agent response exceeds configured limit")
        if result is not None and result.input_tokens + result.output_tokens > 2_147_483_647:
            raise ValueError("agent response usage exceeds storage limit")
        if result is not None and (result.input_tokens < 0 or result.output_tokens < 0):
            raise ValueError("agent response usage is invalid")
        if result is not None:
            try:
                safe_response_id = local_text(result.provider_response_id, allow_empty=True)
            except ValueError:
                raise ValueError("agent provider response ID is invalid") from None
            if safe_response_id != result.provider_response_id or not re.fullmatch(
                r"[A-Za-z0-9._:/+\-]{0,200}", safe_response_id
            ):
                raise ValueError("agent provider response ID is invalid")
        ticks = result.cost_usd_ticks if result is not None else None
        if ticks is not None and (
            prepared.provider != "grok"
            or type(ticks) is not int
            or not 0 <= ticks <= 9_223_372_036_854_775_807
        ):
            raise ValueError("agent provider cost is invalid")
        cost = Decimal(ticks) / Decimal(10_000_000_000) if ticks is not None else None
        body_digest = _digest(body) if body is not None else None
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            turn = (
                connection.execute(
                    text(
                        "SELECT t.id,t.status,t.source_kind,c.work_order_id,c.id AS conversation_id "
                        "FROM task_conversation_turns t JOIN task_conversations c "
                        "ON c.project_id=t.project_id AND c.id=t.conversation_id "
                        "WHERE t.project_id=:project_id AND t.id=:id FOR UPDATE OF t"
                    ),
                    {"project_id": project_id, "id": prepared.run_id},
                )
                .mappings()
                .one()
            )
            if turn["status"] != "running":
                raise Conflict("conversation turn state changed")
            if (
                result is not None
                and status == "done"
                and turn["source_kind"] != "system_triage"
                and not is_discussion_turn(connection, project_id, prepared.run_id)
            ):
                body, _receipt = task_automation.apply_decision(
                    self.gateway,
                    connection,
                    project_id,
                    prepared.run_id,
                    turn["work_order_id"],
                    prepared.profile_id,
                    result.text,
                )
                body = local_text(body)
                if len(body.encode()) > self.settings.live_text_max_output_bytes:
                    raise ValueError("agent response with automation readback exceeds configured limit")
                body_digest = _digest(body)
            connection.execute(
                text(
                    "UPDATE task_conversation_turns SET status=:status,assistant_text=:body,"
                    "assistant_sha256=:assistant_sha256,"
                    "assistant_actor_id=:assistant_actor_id,reason=:reason,provider_response_id=:response_id,"
                    "input_tokens=:input_tokens,output_tokens=:output_tokens,cost_usd=:cost_usd,"
                    "finished_at=clock_timestamp() WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "status": status,
                    "body": body,
                    "assistant_sha256": body_digest,
                    "assistant_actor_id": f"profile:{prepared.profile_id}" if body else None,
                    "reason": reason,
                    "response_id": result.provider_response_id if result else None,
                    "input_tokens": result.input_tokens if result else None,
                    "output_tokens": result.output_tokens if result else None,
                    "cost_usd": cost,
                    "project_id": project_id,
                    "id": prepared.run_id,
                },
            )
            payload: dict[str, Any] = {
                "conversation_id": str(turn["conversation_id"]),
                "turn_id": str(prepared.run_id),
                "status": status,
                "reason": reason,
                "profile_id": str(prepared.profile_id),
                "assistant_sha256": body_digest,
                "provider_response_id": result.provider_response_id if result else None,
                "input_tokens": result.input_tokens if result else None,
                "output_tokens": result.output_tokens if result else None,
                "cost_usd": str(cost) if cost is not None else None,
                "retry": False,
            }
            if diagnostic_code is not None:
                payload["diagnostic_code"] = diagnostic_code
            self.gateway._event(
                connection,
                project_id,
                turn["work_order_id"],
                None,
                "task_conversation_turn_finished",
                payload,
                actor_id=f"profile:{prepared.profile_id}",
            )

    def _record_progress(self, project_id: UUID, prepared: Prepared, position: int, summary: str) -> None:
        """Save only a completed provider-authored public summary for an active turn."""
        if contains_sensitive_text(summary):
            return
        try:
            visible = local_text(summary[:600])
        except ValueError:
            return
        if len(summary) > 600:
            visible += "…"
        try:
            with self.gateway.engine.begin() as connection:
                active = connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM task_conversation_turns "
                        "WHERE project_id=:p AND id=:id AND status='running')"
                    ),
                    {"p": project_id, "id": prepared.run_id},
                )
                if active:
                    self.gateway._event(
                        connection,
                        project_id,
                        prepared.order_id,
                        None,
                        "task_conversation_progress",
                        {
                            "turn_id": str(prepared.run_id),
                            "position": position,
                            "source": "provider_public_summary",
                            "text": visible,
                        },
                        actor_id=f"profile:{prepared.profile_id}",
                    )
        except SQLAlchemyError:
            _LOG.warning("task conversation progress could not be saved")

    def send(
        self,
        project_id: UUID,
        order_id: UUID,
        data: TurnSend,
        *,
        source_kind: TurnSource = "operator_ui",
        remote_inbound_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        prepared, duplicate = self._prepare(project_id, order_id, data, source_kind, remote_inbound_id)
        if prepared is None:
            return duplicate, False
        progress_position = 0

        def on_progress(summary: str) -> None:
            nonlocal progress_position
            if progress_position >= 8:
                return
            progress_position += 1
            self._record_progress(project_id, prepared, progress_position, summary)

        try:
            request = TextRequest(
                model=prepared.model_id,
                instructions=prepared.instructions,
                prompt=prepared.prompt,
                max_output_tokens=self.settings.live_text_max_output_tokens,
                timeout_seconds=self.settings.task_chat_timeout_seconds,
                request_id=str(prepared.run_id),
                images=prepared.images,
                reasoning_effort=prepared.reasoning_effort,
                progress_callback=on_progress,
            )
            result = self.transport_factory(prepared, self.settings).generate(request)
        except (AuthenticationError, RateLimitError, InvalidRequestError, CodexStreamFailure) as error:
            try:
                self._finish(
                    project_id, prepared, "failed", None, "provider_rejected", _diagnostic_code(error)
                )
            except SQLAlchemyError:
                raise ConversationUnknown(prepared.run_id) from None
            raise ConversationFailed(prepared.run_id) from None
        except Exception as error:
            try:
                self._finish(
                    project_id, prepared, "unknown", None, "provider_outcome_unknown", _diagnostic_code(error)
                )
            except SQLAlchemyError:
                pass
            raise ConversationUnknown(prepared.run_id) from None
        try:
            self._finish(project_id, prepared, "done", result, None)
        except Exception:
            try:
                saved = self._get_profile(project_id, order_id, prepared.profile_id)
                if any(item["id"] == prepared.run_id and item["status"] == "done" for item in saved["turns"]):
                    return {
                        "conversation": saved,
                        "turn": next(item for item in saved["turns"] if item["id"] == prepared.run_id),
                    }, True
                self._finish(project_id, prepared, "unknown", None, "persistence_outcome_unknown")
            except (SQLAlchemyError, Conflict):
                pass
            raise ConversationUnknown(prepared.run_id) from None
        saved = self._get_profile(project_id, order_id, prepared.profile_id)
        return {
            "conversation": saved,
            "turn": next(item for item in saved["turns"] if item["id"] == prepared.run_id),
        }, True

    def reconcile_interrupted(self) -> None:
        with self.gateway.engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT t.id,t.project_id,t.profile_id,t.conversation_id,c.work_order_id "
                        "FROM task_conversation_turns t JOIN task_conversations c "
                        "ON c.project_id=t.project_id AND c.id=t.conversation_id "
                        "WHERE t.status='running' FOR UPDATE OF t"
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                connection.execute(
                    text(
                        "UPDATE task_conversation_turns SET status='unknown',"
                        "reason='process_interrupted',finished_at=clock_timestamp() "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": row["project_id"], "id": row["id"]},
                )
                self.gateway._event(
                    connection,
                    row["project_id"],
                    row["work_order_id"],
                    None,
                    "task_conversation_turn_finished",
                    {
                        "conversation_id": str(row["conversation_id"]),
                        "turn_id": str(row["id"]),
                        "status": "unknown",
                        "reason": "process_interrupted",
                        "profile_id": str(row["profile_id"]),
                        "cost_usd": None,
                        "retry": False,
                    },
                    actor_id=f"profile:{row['profile_id']}",
                )
