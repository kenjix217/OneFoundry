"""Project-level operator conversation with the Chief of Staff."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import coordination_store, memory_store
from onefoundry.chief_workflow_summary import workflow_summary
from onefoundry.coordination_schemas import RuleCreate
from onefoundry.inference import AuthenticationError, InvalidRequestError, RateLimitError, TextRequest
from onefoundry.inference.subscription_codex import CodexStreamFailure
from onefoundry.live_text_store import Prepared, TextTransport, _diagnostic_code, _provider_transport
from onefoundry.memory_policy import work_memory_query
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.profile_text_runtime import resolve as resolve_profile_text
from onefoundry.project_chief_decision import INSTRUCTIONS, parse
from onefoundry.project_plan import read_plan
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict, NotFound
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

Source = Literal["operator_ui", "discord", "wechat"]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidates(connection: Any, project_id: UUID, message: str, limit: int) -> list[dict[str, Any]]:
    # Score title words before limiting so an old order can outrank recent unrelated work.
    rows = connection.execute(
        text(
            "WITH words AS (SELECT word FROM unnest(regexp_split_to_array(lower(:message), "
            "'[^[:alnum:]_]+')) AS word WHERE length(word)>2 GROUP BY word LIMIT 12), "
            "ranked AS (SELECT w.id,w.title,w.instructions,w.acceptance_checks,w.status,"
            "w.profile_id,w.parent_work_order_id,w.created_at, "
            "(SELECT count(*) FROM words WHERE position(word in lower(w.title))>0) "
            "+ CASE WHEN position(w.id::text in lower(:message))>0 THEN 100 ELSE 0 END AS score "
            "FROM work_orders w WHERE w.project_id=:project_id) "
            "SELECT r.id,r.title,r.instructions,r.acceptance_checks,r.status,r.profile_id,"
            "r.parent_work_order_id,p.name AS profile_name "
            "FROM ranked r JOIN profiles p ON p.project_id=:project_id AND p.id=r.profile_id "
            "ORDER BY r.score DESC,r.created_at DESC,r.id DESC LIMIT :limit"
        ),
        {"project_id": project_id, "message": message, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def _create_rule(
    gateway: ToolGateway,
    connection: Any,
    settings: Settings,
    project_id: UUID,
    chief_id: UUID,
    data: RuleCreate,
) -> dict[str, Any]:
    controls = coordination_store.status(connection, settings, project_id)
    if not controls["agent_rules_enabled"]:
        raise Conflict("agent rule management is disabled for this project")
    existing_rules = coordination_store.list_rules(connection, project_id)
    if len(existing_rules) > settings.agent_automation_context_max_items:
        raise Conflict("rule catalogue exceeds chat decision limit; inspect existing rules")
    schedule = data.schedule.model_dump(exclude_none=True) if data.schedule else None
    for existing in existing_rules:
        if (
            existing["signal_type"] == data.signal_type
            and existing["schedule"] == schedule
            and existing["filters"] == data.filters
            and existing["target_profile_id"] == data.target_profile_id
            and existing["title"] == data.title
            and existing["instructions"] == data.instructions
        ):
            raise Conflict("an equivalent rule already exists; revise that rule in Automation")
    return coordination_store.create_rule(
        gateway, connection, project_id, data, actor_id=f"profile:{chief_id}"
    )


class ProjectChiefInbox:
    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        transport_factory: Callable[[Prepared, Settings], TextTransport] | None = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.secrets = ModelSecretStore(settings.model_secret_root)
        self.transport_factory = transport_factory or _provider_transport

    def list_turns(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT id,project_id,client_turn_id,source_kind,status,operator_text,"
                        "assistant_text,reason,action,target_work_order_id,created_work_order_id,created_rule_id,"
                        "created_at,finished_at FROM project_chief_turns WHERE project_id=:p "
                        "ORDER BY created_at DESC,id DESC LIMIT :limit"
                    ),
                    {"p": project_id, "limit": self.settings.task_conversation_max_turns},
                ).mappings()
            ][::-1]

    def get_turn(self, project_id: UUID, turn_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id,project_id,client_turn_id,source_kind,status,operator_text,"
                        "assistant_text,reason,action,target_work_order_id,created_work_order_id,created_rule_id,"
                        "created_at,finished_at,remote_inbound_id FROM project_chief_turns "
                        "WHERE project_id=:p AND id=:id"
                    ),
                    {"p": project_id, "id": turn_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("Chief conversation turn not found")
            return dict(row)

    def enqueue(
        self,
        project_id: UUID,
        client_turn_id: UUID,
        message: str,
        *,
        source: Source = "operator_ui",
        remote_inbound_id: UUID | None = None,
    ) -> dict[str, Any]:
        body = local_text(message)
        if len(body.encode("utf-8")) > self.settings.live_text_max_prompt_bytes:
            raise Conflict("operator message exceeds configured prompt limit")
        if (source == "operator_ui") != (remote_inbound_id is None):
            raise Conflict("Chief conversation source binding is invalid")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            chief_id = chief_profile_id(connection, project_id)
            existing = (
                connection.execute(
                    text(
                        "SELECT id,operator_sha256,source_kind,remote_inbound_id FROM project_chief_turns "
                        "WHERE project_id=:p AND client_turn_id=:client"
                    ),
                    {"p": project_id, "client": client_turn_id},
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["operator_sha256"] != _digest(body)
                    or existing["source_kind"] != source
                    or existing["remote_inbound_id"] != remote_inbound_id
                ):
                    raise Conflict("Chief conversation turn ID was reused for another request")
                return self.get_turn(project_id, existing["id"])
            if remote_inbound_id is not None:
                matched = connection.scalar(
                    text(
                        "SELECT 1 FROM remote_inbound i JOIN remote_channels c "
                        "ON c.project_id=i.project_id AND c.id=i.channel_id "
                        "WHERE i.project_id=:p AND i.id=:inbound AND i.client_turn_id=:client "
                        "AND i.body_sha256=:digest AND i.status='processing' "
                        "AND c.platform=:source AND c.enabled AND c.identity_confirmed "
                        "AND i.sender_id=c.sender_id AND i.chat_id=c.chat_id "
                        "AND i.credential_revision=c.credential_revision AND c.profile_id=:chief"
                    ),
                    {
                        "p": project_id,
                        "inbound": remote_inbound_id,
                        "client": client_turn_id,
                        "digest": _digest(body),
                        "source": source,
                        "chief": chief_id,
                    },
                )
                if matched is None:
                    raise Conflict("confirmed remote operator message changed")
            turn_id = uuid4()
            connection.execute(
                text(
                    "INSERT INTO project_chief_turns (id,project_id,actor_id,profile_id,client_turn_id,"
                    "source_kind,remote_inbound_id,operator_text,operator_sha256,status) VALUES "
                    "(:id,:p,:actor,:profile,:client,:source,:inbound,:body,:digest,'pending')"
                ),
                {
                    "id": turn_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "profile": chief_id,
                    "client": client_turn_id,
                    "source": source,
                    "inbound": remote_inbound_id,
                    "body": body,
                    "digest": _digest(body),
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "project_chief_message_received",
                {"turn_id": str(turn_id), "source": source, "body_sha256": _digest(body)},
            )
        return self.get_turn(project_id, turn_id)

    def _prompt(
        self, connection: Any, row: Any, runtime: Any
    ) -> tuple[str, list[UUID], list[dict[str, Any]]]:
        project_id: UUID = row["project_id"]
        project = (
            connection.execute(
                text("SELECT name,goal,desired_outcome FROM projects WHERE id=:p"), {"p": project_id}
            )
            .mappings()
            .one()
        )
        plan = read_plan(connection, project_id)
        roster = [
            dict(item)
            for item in connection.execute(
                text("SELECT id,name FROM profiles WHERE project_id=:p ORDER BY name,id LIMIT :limit"),
                {"p": project_id, "limit": self.settings.agent_automation_context_max_items},
            ).mappings()
        ]
        candidates = _candidates(
            connection, project_id, row["operator_text"], self.settings.agent_automation_context_max_items
        )
        for candidate in candidates:
            candidate["team_workflow"] = workflow_summary(connection, project_id, candidate["id"])
            instructions = str(candidate["instructions"])
            candidate["instructions"] = (
                "[held: sensitive content]"
                if contains_sensitive_text(instructions)
                else instructions[: self.settings.agent_automation_goal_max_chars]
            )
            candidate["instructions_complete"] = (
                not contains_sensitive_text(instructions)
                and len(instructions) <= self.settings.agent_automation_goal_max_chars
            )
            latest = (
                connection.execute(
                    text(
                        "SELECT r.status,rr.summary FROM runs r LEFT JOIN run_results rr "
                        "ON rr.project_id=r.project_id AND rr.run_id=r.id "
                        "WHERE r.project_id=:p AND r.work_order_id=:work "
                        "ORDER BY r.started_at DESC,r.id DESC LIMIT 1"
                    ),
                    {"p": project_id, "work": candidate["id"]},
                )
                .mappings()
                .one_or_none()
            )
            candidate["run_status"] = latest["status"] if latest else None
            summary = str(latest["summary"]) if latest and latest["summary"] else None
            candidate["result_summary"] = (
                "[held: sensitive content]"
                if summary and contains_sensitive_text(summary)
                else summary[: self.settings.agent_automation_goal_max_chars]
                if summary
                else None
            )
        controls = coordination_store.status(connection, self.settings, project_id)
        rules = [
            {
                "id": str(item["id"]),
                "name": item["name"],
                "signal_type": item["signal_type"],
                "schedule": item["schedule"],
                "target_profile_id": str(item["target_profile_id"]),
                "title": item["title"],
            }
            for item in coordination_store.list_rules(connection, project_id)[
                : self.settings.agent_automation_context_max_items
            ]
        ]
        memories = memory_store.retrieve_memory(
            connection,
            project_id,
            work_memory_query(project["name"], row["operator_text"]),
            5,
            "project" if runtime.memory_scope == "project" else None,
            self.settings,
        )
        history = [
            dict(item)
            for item in connection.execute(
                text(
                    "SELECT operator_text,assistant_text,target_work_order_id,created_work_order_id "
                    "FROM project_chief_turns WHERE project_id=:p AND status='done' AND id<>:id "
                    "ORDER BY created_at DESC,id DESC LIMIT :limit"
                ),
                {"p": project_id, "id": row["id"], "limit": self.settings.agent_automation_context_max_items},
            ).mappings()
        ][::-1]
        state: dict[str, Any] = {
            "project": {
                "name": project["name"],
                "goal": project["goal"],
                "desired_outcome": project["desired_outcome"],
                "plan": plan["body"],
            },
            "agents": roster,
            "work_orders": candidates,
            "coordination": {
                "mode": controls["mode"],
                "paused": controls["paused"],
                "agent_rules_enabled": controls["agent_rules_enabled"],
                "timezone": controls["timezone"],
            },
            "rules": rules,
            "approved_memory": [{"id": str(item["id"]), "text": item["text"]} for item in memories],
            "prior_turns": history,
        }
        instructions = local_text(runtime.instructions) + "\n\n" + INSTRUCTIONS
        while True:
            prompt = "Project state (data): " + json.dumps(state, default=str, ensure_ascii=True)
            prompt += "\n\nOperator: " + row["operator_text"]
            if len(instructions.encode()) + len(prompt.encode()) <= self.settings.live_text_max_prompt_bytes:
                return prompt, [item["id"] for item in candidates], memories
            if state["prior_turns"]:
                state["prior_turns"].pop(0)
            elif state["approved_memory"]:
                state["approved_memory"].pop()
            elif state["work_orders"]:
                state["work_orders"].pop()
                candidates.pop()
            elif state["rules"]:
                state["rules"].pop()
            else:
                raise Conflict("project Chief prompt exceeds configured limit")

    def _claim(self) -> tuple[UUID, Prepared] | None:
        with self.gateway.engine.begin() as connection:
            candidate = (
                connection.execute(
                    text(
                        "SELECT t.id,t.project_id FROM project_chief_turns t "
                        "JOIN projects p ON p.id=t.project_id "
                        "WHERE t.status='pending' AND p.status='active' "
                        "ORDER BY t.created_at,t.id LIMIT 1"
                    )
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None:
                return None
            project_id: UUID = candidate["project_id"]
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text("SELECT * FROM project_chief_turns WHERE project_id=:p AND id=:id FOR UPDATE"),
                    {"p": project_id, "id": candidate["id"]},
                )
                .mappings()
                .one()
            )
            if (
                row["status"] != "pending"
                or connection.scalar(
                    text(
                        "SELECT 1 FROM project_chief_turns WHERE project_id=:p AND status='running' LIMIT 1"
                    ),
                    {"p": project_id},
                )
                is not None
            ):
                return None
            try:
                runtime = resolve_profile_text(connection, project_id, row["profile_id"], self.secrets)
                prompt, candidate_ids, memories = self._prompt(connection, row, runtime)
            except Conflict as error:
                connection.execute(
                    text(
                        "UPDATE project_chief_turns SET status='blocked',reason=:reason,"
                        "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                    ),
                    {"reason": str(error), "p": project_id, "id": row["id"]},
                )
                self.gateway._event(
                    connection,
                    project_id,
                    None,
                    None,
                    "project_chief_turn_finished",
                    {"turn_id": str(row["id"]), "status": "blocked", "reason": str(error)},
                )
                return project_id, Prepared(
                    row["id"],
                    project_id,
                    row["profile_id"],
                    UUID(int=0),
                    "",
                    "",
                    0,
                    None,
                    None,
                    "",
                    "",
                    [],
                )
            connection.execute(
                text(
                    "UPDATE project_chief_turns SET status='running',account_id=:account,"
                    "provider=:provider,model_id=:model,candidate_work_order_ids=:candidates "
                    "WHERE project_id=:p AND id=:id"
                ).bindparams(bindparam("candidates", type_=JSONB)),
                {
                    "account": runtime.account_id,
                    "provider": runtime.provider,
                    "model": runtime.model_id,
                    "candidates": [str(item) for item in candidate_ids],
                    "p": project_id,
                    "id": row["id"],
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "project_chief_turn_started",
                {
                    "turn_id": str(row["id"]),
                    "profile_id": str(row["profile_id"]),
                    "model_id": runtime.model_id,
                    "prompt_sha256": _digest(prompt),
                },
            )
            return project_id, Prepared(
                row["id"],
                project_id,
                runtime.profile_id,
                runtime.account_id,
                runtime.provider,
                runtime.model_id,
                runtime.credential_revision,
                runtime.endpoint,
                runtime.api_key,
                local_text(runtime.instructions) + "\n\n" + INSTRUCTIONS,
                prompt,
                memories,
                runtime.codex_state,
                runtime.grok_state,
            )

    def _finish(
        self,
        project_id: UUID,
        prepared: Prepared,
        status: str,
        raw: str | None,
        reason: str | None,
        result: Any = None,
    ) -> None:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            turn = (
                connection.execute(
                    text("SELECT * FROM project_chief_turns WHERE project_id=:p AND id=:id FOR UPDATE"),
                    {"p": project_id, "id": prepared.run_id},
                )
                .mappings()
                .one()
            )
            if turn["status"] != "running":
                raise Conflict("Chief turn state changed")
            if turn["remote_inbound_id"] is not None:
                current_origin = connection.scalar(
                    text(
                        "SELECT 1 FROM remote_inbound i JOIN remote_channels c "
                        "ON c.project_id=i.project_id AND c.id=i.channel_id "
                        "WHERE i.project_id=:p AND i.id=:inbound AND i.status='processing' "
                        "AND i.client_turn_id=:client AND i.body_sha256=:digest "
                        "AND i.sender_id=c.sender_id AND i.chat_id=c.chat_id "
                        "AND i.credential_revision=c.credential_revision "
                        "AND c.platform=:source AND c.profile_id=:profile "
                        "AND c.enabled AND c.identity_confirmed AND c.credential_ready "
                        "FOR SHARE OF i,c"
                    ),
                    {
                        "p": project_id,
                        "inbound": turn["remote_inbound_id"],
                        "client": turn["client_turn_id"],
                        "digest": turn["operator_sha256"],
                        "source": turn["source_kind"],
                        "profile": turn["profile_id"],
                    },
                )
                if current_origin is None:
                    status, raw, reason = "unknown", None, "channel_binding_changed"
            body: str | None = None
            action: str | None = None
            target: UUID | None = None
            created: UUID | None = None
            created_rule: UUID | None = None
            if status == "done" and raw is not None:
                decision = parse(raw)
                if decision is None:
                    body = "I could not confirm a structured decision. No project change was saved."
                    action = "unavailable"
                else:
                    body = decision.reply
                    action = decision.action
                    allowed = {UUID(item) for item in turn["candidate_work_order_ids"]}
                    if decision.action == "focus_work_order":
                        assert decision.target_work_order_id is not None
                        if decision.target_work_order_id not in allowed:
                            body += "\n\nI could not identify that work order. Please give its exact title."
                            action = "clarify"
                        else:
                            target = decision.target_work_order_id
                            order = (
                                connection.execute(
                                    text(
                                        "SELECT title,status FROM work_orders WHERE project_id=:p AND id=:id"
                                    ),
                                    {"p": project_id, "id": target},
                                )
                                .mappings()
                                .one()
                            )
                            body += f"\n\nDiscussing “{order['title']}” ({order['status']})."
                    elif decision.action == "create_work_order":
                        assert decision.work_order is not None
                        parent = decision.parent_work_order_id
                        if parent is not None and parent not in allowed:
                            body += "\n\nNo work order was saved: the follow-up target was not identified."
                            action = "clarify"
                        elif (
                            connection.scalar(
                                text("SELECT 1 FROM profiles WHERE project_id=:p AND id=:id"),
                                {"p": project_id, "id": decision.work_order.profile_id},
                            )
                            is None
                        ):
                            body += "\n\nNo work order was saved: the chosen agent is unavailable."
                            action = "clarify"
                        elif contains_sensitive_text(decision.work_order.title) or contains_sensitive_text(
                            decision.work_order.instructions
                        ):
                            body += "\n\nNo work order was saved: the assignment contains sensitive text."
                            action = "clarify"
                        else:
                            created = uuid4()
                            controls = coordination_store.status(connection, self.settings, project_id)
                            ready = bool(
                                decision.request_start
                                and controls["mode"] == "auto"
                                and not controls["paused"]
                            )
                            order_status = "ready" if ready else "draft"
                            connection.execute(
                                text(
                                    "INSERT INTO work_orders (id,project_id,actor_id,title,instructions,"
                                    "profile_id,deliverable_kind,acceptance_checks,parent_work_order_id,"
                                    "origin_chief_turn_id,status) VALUES (:id,:p,:actor,:title,:instructions,"
                                    ":profile,:kind,:checks,:parent,:turn,:status)"
                                ).bindparams(bindparam("checks", type_=JSONB)),
                                {
                                    "id": created,
                                    "p": project_id,
                                    "actor": f"profile:{prepared.profile_id}",
                                    "title": decision.work_order.title,
                                    "instructions": decision.work_order.instructions,
                                    "profile": decision.work_order.profile_id,
                                    "kind": decision.work_order.deliverable_kind,
                                    "checks": decision.work_order.acceptance_checks,
                                    "parent": parent,
                                    "turn": prepared.run_id,
                                    "status": order_status,
                                },
                            )
                            self.gateway._event(
                                connection,
                                project_id,
                                created,
                                None,
                                "work_order_created",
                                {
                                    "status": order_status,
                                    "profile_id": str(decision.work_order.profile_id),
                                    "origin_chief_turn_id": str(prepared.run_id),
                                    "parent_work_order_id": str(parent) if parent else None,
                                },
                                actor_id=f"profile:{prepared.profile_id}",
                            )
                            target = created
                            if ready and decision.work_order.deliverable_kind != "code_change":
                                launch_id = uuid4()
                                connection.execute(
                                    text(
                                        "INSERT INTO coordination_intents "
                                        "(id,project_id,actor_id,step_key,action_kind,work_order_id) "
                                        "VALUES (:id,:p,:actor,'auto_start','auto_start',:work)"
                                    ),
                                    {
                                        "id": launch_id,
                                        "p": project_id,
                                        "actor": f"profile:{prepared.profile_id}",
                                        "work": created,
                                    },
                                )
                                body += (
                                    f"\n\nSaved “{decision.work_order.title}” as ready; launch queued. "
                                    "The agent has not run yet."
                                )
                            elif ready:
                                body += (
                                    f"\n\nSaved “{decision.work_order.title}” as ready for coding. "
                                    "No coding attempt was launched."
                                )
                            else:
                                body += (
                                    f"\n\nSaved “{decision.work_order.title}” as a draft. "
                                    "Start it from Work orders."
                                )
                    elif decision.action == "create_rule":
                        assert decision.rule is not None
                        try:
                            with connection.begin_nested():
                                saved_rule = _create_rule(
                                    self.gateway,
                                    connection,
                                    self.settings,
                                    project_id,
                                    prepared.profile_id,
                                    decision.rule,
                                )
                        except Conflict as error:
                            body += f"\n\nNo Automation rule was saved: {error}."
                            action = "clarify"
                        else:
                            created_rule = saved_rule["id"]
                            body += (
                                f"\n\nAutomation rule created: {saved_rule['name']} "
                                f"(version {saved_rule['version']}, {saved_rule['signal_type']})."
                            )
            connection.execute(
                text(
                    "UPDATE project_chief_turns SET status=:status,assistant_text=:body,"
                    "assistant_sha256=:digest,reason=:reason,action=:action,"
                    "target_work_order_id=:target,created_work_order_id=:created,created_rule_id=:rule,"
                    "provider_response_id=:response,input_tokens=:input_tokens,"
                    "output_tokens=:output_tokens,finished_at=clock_timestamp() "
                    "WHERE project_id=:p AND id=:id"
                ),
                {
                    "status": status,
                    "body": body,
                    "digest": _digest(body) if body else None,
                    "reason": reason,
                    "action": action,
                    "target": target,
                    "created": created,
                    "rule": created_rule,
                    "response": result.provider_response_id if result else None,
                    "input_tokens": result.input_tokens if result else None,
                    "output_tokens": result.output_tokens if result else None,
                    "p": project_id,
                    "id": prepared.run_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                target,
                None,
                "project_chief_turn_finished",
                {
                    "turn_id": str(prepared.run_id),
                    "status": status,
                    "action": action,
                    "created_work_order_id": str(created) if created else None,
                    "created_rule_id": str(created_rule) if created_rule else None,
                    "assistant_sha256": _digest(body) if body else None,
                    "reason": reason,
                },
            )

    def process_one(self) -> dict[str, Any] | None:
        claimed = self._claim()
        if claimed is None:
            return None
        project_id, prepared = claimed
        if not prepared.model_id:  # Configuration was blocked before provider dispatch.
            return self.get_turn(project_id, prepared.run_id)
        try:
            result = self.transport_factory(prepared, self.settings).generate(
                TextRequest(
                    model=prepared.model_id,
                    instructions=prepared.instructions,
                    prompt=prepared.prompt,
                    max_output_tokens=self.settings.live_text_max_output_tokens,
                    timeout_seconds=self.settings.live_text_timeout_seconds,
                    request_id=str(prepared.run_id),
                )
            )
        except (AuthenticationError, RateLimitError, InvalidRequestError, CodexStreamFailure) as error:
            self._finish(project_id, prepared, "failed", None, _diagnostic_code(error))
        except Exception as error:
            self._finish(project_id, prepared, "unknown", None, _diagnostic_code(error))
        else:
            try:
                self._finish(project_id, prepared, "done", result.text, None, result)
            except Exception:
                # A saved decision may already exist. Never repeat the provider result or action.
                with self.gateway.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE project_chief_turns SET status='unknown',"
                            "reason='persistence_outcome_unknown',finished_at=clock_timestamp() "
                            "WHERE project_id=:p AND id=:id AND status='running'"
                        ),
                        {"p": project_id, "id": prepared.run_id},
                    )
        return self.get_turn(project_id, prepared.run_id)

    def reconcile_interrupted(self) -> None:
        with self.gateway.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "UPDATE project_chief_turns SET status='unknown',reason='process_interrupted',"
                    "finished_at=clock_timestamp() WHERE status='running' RETURNING id,project_id"
                )
            ).mappings()
            for row in rows:
                self.gateway._event(
                    connection,
                    row["project_id"],
                    None,
                    None,
                    "project_chief_turn_finished",
                    {"turn_id": str(row["id"]), "status": "unknown", "reason": "process_interrupted"},
                )
