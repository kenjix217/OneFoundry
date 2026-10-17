"""Validate one agent decision and apply its local project change with turn lineage."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import coordination_store
from onefoundry.coordination_schemas import RuleCreate, RuleEnabled, RuleRevise
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import chief_profile_id
from onefoundry.project_plan import ProjectPlanUpdate, read_plan, update_plan
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.schemas import ProjectBrief
from onefoundry.settings import Settings
from onefoundry.task_decision_validation import rejected_action_reply, validation_errors
from onefoundry.workflow_schemas import StrictModel, WorkOrderCreate, local_text

DecisionKind = Literal[
    "none",
    "clarify",
    "create_rule",
    "revise_rule",
    "disable_rule",
    "update_project_goal",
    "update_project_plan",
    "delegate_automation",
    "delegate_team",
]


class TeamAssignment(StrictModel):
    work_order: WorkOrderCreate
    autonomy: Literal["draft", "auto_start"] = "draft"


class AgentDecision(StrictModel):
    reply: str = Field(min_length=1, max_length=16000)
    decision: DecisionKind
    create: RuleCreate | None = None
    revise: RuleRevise | None = None
    rule_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    project_brief: ProjectBrief | None = None
    project_plan: ProjectPlanUpdate | None = None
    delegate: WorkOrderCreate | None = None
    delegate_autonomy: Literal["draft", "auto_start"] | None = None
    team: list[TeamAssignment] | None = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def exact_command(self) -> "AgentDecision":
        required = {
            "create": self.decision == "create_rule",
            "revise": self.decision == "revise_rule",
            "rule_id": self.decision in {"revise_rule", "disable_rule"},
            "expected_version": self.decision == "disable_rule",
            "delegate": self.decision == "delegate_automation",
            "team": self.decision == "delegate_team",
        }
        if any((getattr(self, key) is not None) != needed for key, needed in required.items()):
            raise ValueError("decision fields do not match the selected command")
        if self.decision == "update_project_goal" and self.project_brief is None:
            raise ValueError("project goal update needs the full project brief")
        if self.decision == "update_project_plan" and self.project_plan is None:
            raise ValueError("project plan update needs the full plan")
        if self.project_brief is not None and self.decision not in {
            "update_project_goal",
            "update_project_plan",
            "create_rule",
            "revise_rule",
            "disable_rule",
            "delegate_team",
            "delegate_automation",
        }:
            raise ValueError("project brief cannot accompany this command")
        if self.project_plan is not None and self.decision not in {
            "update_project_plan",
            "update_project_goal",
            "create_rule",
            "revise_rule",
            "disable_rule",
            "delegate_team",
            "delegate_automation",
        }:
            raise ValueError("project plan cannot accompany this command")
        if self.delegate_autonomy is not None and self.decision != "delegate_automation":
            raise ValueError("delegate autonomy cannot accompany this command")
        self.reply = local_text(self.reply)
        return self


def parse_response(raw: str) -> AgentDecision | None:
    try:
        return AgentDecision.model_validate_json(raw)
    except (ValueError, ValidationError):
        return None


def invalid_reply(raw: str) -> str:
    """Keep a readable reply when an action proposal fails validation."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return local_text(raw)
    if isinstance(payload, dict) and isinstance(payload.get("reply"), str):
        try:
            return local_text(payload["reply"])
        except ValueError:
            pass
    return local_text(raw)


def _attempted_action(raw: str) -> bool:
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("decision"), str)


def decision_context(connection: Any, project_id: UUID, profile_id: UUID, settings: Settings) -> str:
    project = (
        connection.execute(
            text("SELECT goal,desired_outcome,version FROM projects WHERE id=:p"), {"p": project_id}
        )
        .mappings()
        .one()
    )
    plan = read_plan(connection, project_id)
    controls = coordination_store.status(connection, settings, project_id)
    current_profile = connection.scalar(
        text("SELECT name FROM profiles WHERE project_id=:p AND id=:profile"),
        {"p": project_id, "profile": profile_id},
    )
    profiles = (
        connection.execute(
            text("SELECT id,name FROM profiles WHERE project_id=:p ORDER BY name,id LIMIT :limit"),
            {"p": project_id, "limit": settings.agent_automation_context_max_items + 1},
        )
        .mappings()
        .all()
    )
    rules = coordination_store.list_rules(connection, project_id)
    catalogue = [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "enabled": row["enabled"],
            "version": row["version"],
            "source": row["source"],
            "signal_type": row["signal_type"],
            "schedule": row["schedule"],
            "filters": row["filters"],
            "target_profile_id": str(row["target_profile_id"]),
            "title": row["title"],
            "instructions": row["instructions"][: settings.agent_automation_goal_max_chars],
            "instructions_complete": (len(row["instructions"]) <= settings.agent_automation_goal_max_chars),
            "deliverable_kind": row["deliverable_kind"],
            "acceptance_checks": row["acceptance_checks"],
            "autonomy": row["autonomy"],
            "max_per_day": row["max_per_day"],
            "max_model_attempts": row["max_model_attempts"],
            "quiet_hours": row["quiet_hours"],
            "max_chain_depth": row["max_chain_depth"],
        }
        for row in rules[: settings.agent_automation_context_max_items]
    ]
    state = {
        "project_goal": str(project["goal"])[: settings.agent_automation_goal_max_chars],
        "desired_outcome": str(project["desired_outcome"])[: settings.agent_automation_goal_max_chars],
        "goal_complete": len(project["goal"]) <= settings.agent_automation_goal_max_chars
        and len(project["desired_outcome"]) <= settings.agent_automation_goal_max_chars,
        "project_version": project["version"],
        "project_plan": plan["body"],
        "plan_version": plan["version"],
        "timezone": controls["timezone"],
        "coordination_mode": controls["mode"],
        "coordination_paused": controls["paused"],
        "agent_rules_enabled": controls["agent_rules_enabled"],
        "current_profile_id": str(profile_id),
        "current_profile_name": current_profile,
        "profiles": [
            {"id": str(row["id"]), "name": row["name"]}
            for row in profiles[: settings.agent_automation_context_max_items]
        ],
        "profile_catalogue_complete": len(profiles) <= settings.agent_automation_context_max_items,
        "rules": catalogue,
        "rule_catalogue_complete": len(rules) <= settings.agent_automation_context_max_items,
        "signal_types": list(controls["signal_types"]),
    }
    return "Project state (data): " + json.dumps(state, default=str, ensure_ascii=True, separators=(",", ":"))


DECISION_INSTRUCTIONS = """
Decide whether the goal needs a durable Automation rule or agent work.
Return only one JSON object with short `reply` and `decision`: none, clarify,
create_rule, revise_rule, disable_rule, update_project_goal,
update_project_plan, delegate_automation, or delegate_team. Check existing
rules and child work; none changes nothing, clarify asks for missing details.
create_rule: `create` with name, signal_type, target_profile_id, title,
instructions. revise_rule: `rule_id`, full `revise`, expected_version.
disable_rule: `rule_id`, `expected_version`. Goal/plan updates need full
versioned `project_brief`/`project_plan`; a rule may carry both atomically.
Only Chief may delegate_automation with `delegate` work order, or delegate_team
with `team`: [{"work_order":{...},"autonomy":"draft"}]. Each work_order has
`title`, `instructions`, `profile_id` (exact UUID from Project state profiles),
`deliverable_kind`, `acceptance_checks` (list of strings); no other keys.
Put inputs and dependencies in `instructions`. Allowed autonomy: "draft" or
"auto_start"; manual means "draft". If profile ID is missing, clarify.
auto_start queues live text only in Auto mode; code needs the separate runner.
Saved assignments have not run. Never claim unsaved actions or approve outbound.
""".strip()


def _digest(value: AgentDecision | None, raw: str) -> str:
    material = value.model_dump_json() if value is not None else raw
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _update_project_goal(connection: Any, project_id: UUID, data: ProjectBrief) -> dict[str, Any]:
    changed = connection.execute(
        text(
            "UPDATE projects SET goal=:goal,desired_outcome=:outcome,version=version+1 "
            "WHERE id=:p AND status='active' AND version=:v RETURNING version"
        ),
        {
            "p": project_id,
            "goal": data.goal,
            "outcome": data.desired_outcome,
            "v": data.expected_version,
        },
    ).scalar_one_or_none()
    if changed is None:
        raise Conflict("project goal changed; reload before saving")
    return {"project_version": int(changed)}


def _save_project_goal(gateway: Any, connection: Any, project_id: UUID, data: ProjectBrief) -> int:
    if gateway.live_text_settings is None:
        raise Conflict("agent automation settings unavailable")
    full_goal = (
        connection.execute(
            text("SELECT goal,desired_outcome FROM projects WHERE id=:p"),
            {"p": project_id},
        )
        .mappings()
        .one()
    )
    max_chars = gateway.live_text_settings.agent_automation_goal_max_chars
    if len(full_goal["goal"]) > max_chars or len(full_goal["desired_outcome"]) > max_chars:
        raise Conflict("project goal exceeds chat context limit; edit it in the project brief")
    return int(_update_project_goal(connection, project_id, data)["project_version"])


def _save_project_plan(connection: Any, project_id: UUID, data: ProjectPlanUpdate, actor_id: str) -> int:
    return int(update_plan(connection, project_id, data, actor_id)["version"])


def _delegate_automation(
    gateway: Any,
    connection: Any,
    project_id: UUID,
    parent_work_order_id: UUID,
    source_run_id: UUID | None,
    chief_id: UUID,
    data: WorkOrderCreate,
    autonomy: Literal["draft", "auto_start"],
    mode: str,
) -> dict[str, Any]:
    if chief_id != chief_profile_id(connection, project_id):
        raise Conflict("only Chief of Staff may assign another agent")
    if data.profile_id == chief_id:
        raise Conflict("choose another project agent for the assignment")
    target = connection.scalar(
        text("SELECT name FROM profiles WHERE project_id=:p AND id=:profile"),
        {"p": project_id, "profile": data.profile_id},
    )
    if target is None:
        raise NotFound("target agent is not in this project")
    if contains_sensitive_text(data.title) or contains_sensitive_text(data.instructions):
        raise Conflict("assignment contains sensitive text")
    duplicate = connection.scalar(
        text(
            "SELECT 1 FROM work_orders WHERE project_id=:p AND parent_work_order_id=:parent "
            "AND profile_id=:profile AND lower(title)=lower(:title) LIMIT 1"
        ),
        {"p": project_id, "parent": parent_work_order_id, "profile": data.profile_id, "title": data.title},
    )
    if duplicate:
        raise Conflict("this agent already has a child assignment with that title")
    order_id = uuid4()
    status = "ready" if autonomy == "auto_start" and mode == "auto" else "draft"
    live_text_queued = status == "ready" and data.deliverable_kind != "code_change"
    row = (
        connection.execute(
            text(
                "INSERT INTO work_orders "
                "(id,project_id,actor_id,title,instructions,profile_id,deliverable_kind,"
                "acceptance_checks,parent_work_order_id,source_run_id,status) VALUES "
                "(:id,:p,:actor,:title,:instructions,:profile,:kind,:checks,:parent,:run,:status) "
                "RETURNING id,status,version"
            ).bindparams(bindparam("checks", type_=JSONB)),
            {
                "id": order_id,
                "p": project_id,
                "actor": f"profile:{chief_id}",
                "title": data.title,
                "instructions": data.instructions,
                "profile": data.profile_id,
                "kind": data.deliverable_kind,
                "checks": data.acceptance_checks,
                "parent": parent_work_order_id,
                "run": source_run_id,
                "status": status,
            },
        )
        .mappings()
        .one()
    )
    if row["status"] != status:
        raise Conflict("assigned work order status was not confirmed")
    gateway._event(
        connection,
        project_id,
        order_id,
        None,
        "work_order_created",
        {
            "status": status,
            "profile_id": str(data.profile_id),
            "parent_work_order_id": str(parent_work_order_id),
            "source_run_id": str(source_run_id) if source_run_id else None,
        },
        actor_id=f"profile:{chief_id}",
    )
    if live_text_queued:
        launch_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO coordination_intents "
                "(id,project_id,actor_id,step_key,action_kind,work_order_id) "
                "VALUES (:id,:p,:actor,'auto_start','auto_start',:work)"
            ),
            {"id": launch_id, "p": project_id, "actor": f"profile:{chief_id}", "work": order_id},
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "coordination_launch_queued",
            {"intent_id": str(launch_id), "source": "chief_assignment"},
            actor_id=f"profile:{chief_id}",
        )
    return {
        "id": order_id,
        "status": status,
        "live_text_queued": live_text_queued,
        "version": row["version"],
        "profile_name": target,
        "title": data.title,
    }


def _require_current_remote_origin(connection: Any, project_id: UUID, turn_id: UUID) -> None:
    turn = (
        connection.execute(
            text(
                "SELECT source_kind,remote_inbound_id FROM task_conversation_turns "
                "WHERE project_id=:p AND id=:turn"
            ),
            {"p": project_id, "turn": turn_id},
        )
        .mappings()
        .one()
    )
    if turn["source_kind"] not in {"discord", "wechat"}:
        return
    matched = connection.scalar(
        text(
            "SELECT 1 FROM task_conversation_turns t "
            "JOIN task_conversations tc ON tc.project_id=t.project_id AND tc.id=t.conversation_id "
            "JOIN remote_inbound i ON i.project_id=t.project_id AND i.id=t.remote_inbound_id "
            "JOIN remote_channels c ON c.project_id=i.project_id AND c.id=i.channel_id "
            "WHERE t.project_id=:p AND t.id=:turn AND i.status='processing' "
            "AND i.body_sha256=t.operator_sha256 AND i.client_turn_id=t.client_turn_id "
            "AND i.work_order_id=tc.work_order_id AND c.work_order_id=tc.work_order_id "
            "AND c.profile_id=t.profile_id AND c.platform=t.source_kind "
            "AND c.enabled AND c.identity_confirmed "
            "AND i.sender_id=c.sender_id AND i.chat_id=c.chat_id FOR SHARE OF i,c"
        ),
        {"p": project_id, "turn": turn_id},
    )
    if matched is None:
        raise Conflict("remote operator binding changed during the reply")


def apply_decision(
    gateway: Any,
    connection: Any,
    project_id: UUID,
    execution_id: UUID,
    work_order_id: UUID,
    profile_id: UUID,
    raw: str,
    *,
    execution_kind: Literal["turn", "run"] = "turn",
) -> tuple[str, dict[str, Any]]:
    decision = parse_response(raw)
    actor_id = f"profile:{profile_id}"
    kind = decision.decision if decision else "unavailable"
    receipt: dict[str, Any] = {"decision": kind, "status": "unavailable"}
    body = decision.reply if decision else invalid_reply(raw)
    if decision is None:
        action_attempted = _attempted_action(raw)
        receipt["error_code"] = "invalid_action_proposal" if action_attempted else "unstructured_reply"
        if action_attempted:
            receipt["status"] = "rejected"
            issues = validation_errors(raw)
            if gateway.live_text_settings is not None:
                issues = issues[: gateway.live_text_settings.agent_automation_context_max_items]
            receipt["validation_errors"] = issues
            body = rejected_action_reply()
        source_kind = (
            connection.scalar(
                text("SELECT source_kind FROM task_conversation_turns WHERE project_id=:p AND id=:turn"),
                {"p": project_id, "turn": execution_id},
            )
            if execution_kind == "turn"
            else "agent_run"
        )
        if source_kind in {"discord", "wechat"} and action_attempted:
            body += "\n\nThe proposed action was invalid; no assignment or project change was saved."
    elif kind in {"none", "clarify"}:
        receipt["status"] = kind
    else:
        try:
            with connection.begin_nested():
                controls = coordination_store.status(connection, gateway.live_text_settings, project_id)
                if (
                    kind in {"create_rule", "revise_rule", "disable_rule"}
                    and not controls["agent_rules_enabled"]
                ):
                    raise Conflict("agent rule management is disabled for this project")
                active = connection.scalar(text("SELECT status FROM projects WHERE id=:p"), {"p": project_id})
                if active != "active":
                    raise Conflict("project is not active")
                if execution_kind == "turn":
                    _require_current_remote_origin(connection, project_id, execution_id)
                else:
                    binding = connection.scalar(
                        text(
                            "SELECT 1 FROM runs WHERE project_id=:p AND id=:run AND work_order_id=:work "
                            "AND profile_id=:profile AND mode='live_text' AND status='running'"
                        ),
                        {
                            "p": project_id,
                            "run": execution_id,
                            "work": work_order_id,
                            "profile": profile_id,
                        },
                    )
                    if binding is None:
                        raise Conflict("agent run binding changed before saving")
                goal_version: int | None = None
                plan_version: int | None = None
                if decision.project_brief is not None and kind != "update_project_goal":
                    goal_version = _save_project_goal(gateway, connection, project_id, decision.project_brief)
                if decision.project_plan is not None and kind != "update_project_plan":
                    plan_version = _save_project_plan(connection, project_id, decision.project_plan, actor_id)
                result: dict[str, Any]
                if kind == "create_rule":
                    assert decision.create is not None
                    if gateway.live_text_settings is None:
                        raise Conflict("agent automation settings unavailable")
                    rule_count = connection.scalar(
                        text("SELECT count(*) FROM coordination_rules WHERE project_id=:p"),
                        {"p": project_id},
                    )
                    if int(rule_count or 0) > gateway.live_text_settings.agent_automation_context_max_items:
                        raise Conflict("rule catalogue exceeds chat decision limit; inspect existing rules")
                    schedule = (
                        decision.create.schedule.model_dump(exclude_none=True)
                        if decision.create.schedule
                        else None
                    )
                    for existing in coordination_store.list_rules(connection, project_id):
                        if (
                            existing["signal_type"] == decision.create.signal_type
                            and existing["schedule"] == schedule
                            and existing["filters"] == decision.create.filters
                            and existing["target_profile_id"] == decision.create.target_profile_id
                            and existing["title"] == decision.create.title
                            and existing["instructions"] == decision.create.instructions
                        ):
                            raise Conflict("an equivalent rule already exists; revise that rule instead")
                    result = coordination_store.create_rule(
                        gateway, connection, project_id, decision.create, actor_id=actor_id
                    )
                elif kind == "revise_rule":
                    assert decision.rule_id is not None and decision.revise is not None
                    if gateway.live_text_settings is None:
                        raise Conflict("agent automation settings unavailable")
                    existing_rules = coordination_store.list_rules(connection, project_id)
                    if len(existing_rules) > gateway.live_text_settings.agent_automation_context_max_items:
                        raise Conflict("rule catalogue exceeds chat decision limit; inspect existing rules")
                    selected_rule = next(
                        (row for row in existing_rules if row["id"] == decision.rule_id), None
                    )
                    if selected_rule is None:
                        raise NotFound("rule is not in this project")
                    if (
                        len(selected_rule["instructions"])
                        > gateway.live_text_settings.agent_automation_goal_max_chars
                    ):
                        raise Conflict("rule details exceed chat context limit; edit it in Automation")
                    result = coordination_store.revise_rule(
                        gateway,
                        connection,
                        project_id,
                        decision.rule_id,
                        decision.revise,
                        actor_id=actor_id,
                    )
                elif kind == "disable_rule":
                    assert decision.rule_id is not None and decision.expected_version is not None
                    result = coordination_store.set_rule_enabled(
                        gateway,
                        connection,
                        project_id,
                        decision.rule_id,
                        RuleEnabled(enabled=False, expected_version=decision.expected_version),
                        actor_id=actor_id,
                    )
                elif kind == "delegate_automation":
                    assert decision.delegate is not None
                    result = _delegate_automation(
                        gateway,
                        connection,
                        project_id,
                        work_order_id,
                        execution_id if execution_kind == "run" else None,
                        profile_id,
                        decision.delegate,
                        decision.delegate_autonomy or "draft",
                        str(controls["mode"]),
                    )
                elif kind == "delegate_team":
                    assert decision.team is not None
                    settings = gateway.live_text_settings
                    if settings is None:
                        raise Conflict("agent assignment settings unavailable")
                    if len(decision.team) > settings.agent_automation_context_max_items:
                        raise Conflict("too many assignments in one reply")
                    if len(
                        {
                            (item.work_order.profile_id, item.work_order.title.casefold())
                            for item in decision.team
                        }
                    ) != len(decision.team):
                        raise Conflict("the reply repeats an agent assignment")
                    assignments = [
                        _delegate_automation(
                            gateway,
                            connection,
                            project_id,
                            work_order_id,
                            execution_id if execution_kind == "run" else None,
                            profile_id,
                            item.work_order,
                            item.autonomy,
                            str(controls["mode"]),
                        )
                        for item in decision.team
                    ]
                    result = {"assignments": assignments}
                elif kind == "update_project_plan":
                    assert decision.project_plan is not None
                    result = {
                        "plan_version": _save_project_plan(
                            connection, project_id, decision.project_plan, actor_id
                        )
                    }
                else:
                    assert decision.project_brief is not None
                    result = {
                        "project_version": _save_project_goal(
                            gateway, connection, project_id, decision.project_brief
                        )
                    }
                receipt["status"] = "saved"
                if goal_version is not None:
                    receipt["project_version"] = goal_version
                if plan_version is not None:
                    receipt["plan_version"] = plan_version
                if kind == "update_project_goal":
                    receipt["project_version"] = result["project_version"]
                    body += f"\n\nProject goal saved (version {result['project_version']})."
                elif kind == "update_project_plan":
                    receipt["plan_version"] = result["plan_version"]
                    body += f"\n\nProject plan saved (version {result['plan_version']})."
                elif kind == "delegate_automation":
                    receipt["assigned_work_order_id"] = result["id"]
                    receipt["assignments"] = [result]
                    if result["live_text_queued"]:
                        body += (
                            f"\n\nAssignment queued for {result['profile_name']}: {result['title']}. "
                            "The agent has not run yet; Automation will attempt a bounded launch."
                        )
                    elif result["status"] == "ready":
                        body += (
                            f"\n\nCoding assignment ready for {result['profile_name']}: {result['title']}. "
                            "No live-text launch was queued; start a coding runner from Work orders."
                        )
                    else:
                        body += (
                            f"\n\nDraft assignment saved for {result['profile_name']}: "
                            f"{result['title']}. The agent has not run yet; start it from Work orders."
                        )
                elif kind == "delegate_team":
                    receipt["assignments"] = result["assignments"]
                    receipt["assigned_work_order_id"] = result["assignments"][0]["id"]
                    ready_count = sum(item["live_text_queued"] for item in result["assignments"])
                    coding_count = sum(
                        item["status"] == "ready" and not item["live_text_queued"]
                        for item in result["assignments"]
                    )
                    body += (
                        f"\n\nSaved {len(result['assignments'])} linked assignments "
                        f"({ready_count} queued for bounded live-text launch, "
                        f"{coding_count} ready for a separate coding runner, "
                        f"{len(result['assignments']) - ready_count - coding_count} drafts). "
                        "No assigned agent has completed its work yet."
                    )
                else:
                    receipt["rule_id"] = result["id"]
                    receipt["rule_version"] = result["version"]
                    verb = {"create_rule": "created", "revise_rule": "revised", "disable_rule": "disabled"}[
                        kind
                    ]
                    body += (
                        f"\n\nAutomation rule {verb}: {result['name']} "
                        f"(version {result['version']}, {result['signal_type']}, "
                        f"target {result['target_profile_id']}, {result['autonomy']}, "
                        f"{'enabled' if result['enabled'] else 'disabled'})."
                    )
                    if goal_version is not None:
                        body += f" Project goal saved (version {goal_version})."
                    if plan_version is not None:
                        body += f" Project plan saved (version {plan_version})."
                if kind == "update_project_goal" and plan_version is not None:
                    body += f" Project plan saved (version {plan_version})."
                if kind == "update_project_plan" and goal_version is not None:
                    body += f" Project goal saved (version {goal_version})."
        except (Conflict, NotFound) as error:
            receipt["status"] = "rejected"
            receipt["error_code"] = "conflict" if isinstance(error, Conflict) else "not_found"
            body += f"\n\nNo project or Automation change was saved: {error}."
    decision_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO task_automation_decisions "
            "(id,project_id,turn_id,run_id,actor_id,decision,status,arguments_sha256,"
            "rule_id,rule_version,project_version,plan_version,assigned_work_order_id,error_code) VALUES "
            "(:id,:p,:turn,:run,:actor,:decision,:status,:digest,:rule,:rv,:pv,:plan,:assigned,:error)"
        ),
        {
            "id": decision_id,
            "p": project_id,
            "turn": execution_id if execution_kind == "turn" else None,
            "run": execution_id if execution_kind == "run" else None,
            "actor": actor_id,
            "decision": kind,
            "status": receipt["status"],
            "digest": _digest(decision, raw),
            "rule": receipt.get("rule_id"),
            "rv": receipt.get("rule_version"),
            "pv": receipt.get("project_version"),
            "plan": receipt.get("plan_version"),
            "assigned": receipt.get("assigned_work_order_id"),
            "error": receipt.get("error_code"),
        },
    )
    for position, assigned in enumerate(receipt.get("assignments", []), start=1):
        connection.execute(
            text(
                "INSERT INTO task_assignment_links "
                "(decision_id,project_id,position,work_order_id,actor_id) "
                "VALUES (:decision,:p,:position,:work,:actor)"
            ),
            {
                "decision": decision_id,
                "p": project_id,
                "position": position,
                "work": assigned["id"],
                "actor": actor_id,
            },
        )
    gateway._event(
        connection,
        project_id,
        work_order_id,
        execution_id if execution_kind == "run" else None,
        "task_automation_decision",
        {
            "turn_id": str(execution_id) if execution_kind == "turn" else None,
            "run_id": str(execution_id) if execution_kind == "run" else None,
            "decision": kind,
            "status": receipt["status"],
            "rule_id": str(receipt["rule_id"]) if receipt.get("rule_id") else None,
            "rule_version": receipt.get("rule_version"),
            "project_version": receipt.get("project_version"),
            "plan_version": receipt.get("plan_version"),
            "assigned_work_order_id": (
                str(receipt["assigned_work_order_id"]) if receipt.get("assigned_work_order_id") else None
            ),
            "error_code": receipt.get("error_code"),
            "validation_errors": receipt.get("validation_errors", []),
        },
        actor_id=actor_id,
    )
    return body, receipt
