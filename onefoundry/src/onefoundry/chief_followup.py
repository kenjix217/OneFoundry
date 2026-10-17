"""Resume a Chief task once for each confirmed child outcome.

The saved assignment decision, child version, and conversation turn bind this
continuation. An interrupted model call becomes unknown and is never resent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import chief_profile_id
from onefoundry.task_conversation_schemas import NewConversation, TurnSend

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

NAMESPACE = UUID("6488b63d-e49c-43a2-89cd-e50b6b459980")


def enqueue(gateway: ToolGateway, connection: Any, project_id: UUID, child_id: UUID) -> None:
    """Save one follow-up intent in the child state transition transaction."""
    settings = gateway.live_text_settings
    if settings is None or settings.coordination_mode != "auto":
        return
    row = (
        connection.execute(
            text(
                "SELECT o.parent_work_order_id,o.status,o.version,d.id AS decision_id,"
                "parent.profile_id AS parent_profile_id,"
                "owner.execution_mode AS parent_session_mode FROM work_orders o "
                "JOIN task_assignment_links l ON l.project_id=o.project_id AND l.work_order_id=o.id "
                "JOIN task_automation_decisions d ON d.project_id=l.project_id AND d.id=l.decision_id "
                "JOIN work_orders parent ON parent.project_id=o.project_id "
                "AND parent.id=o.parent_work_order_id "
                "LEFT JOIN task_sessions owner ON owner.project_id=parent.project_id "
                "AND owner.work_order_id=parent.id AND owner.is_order_owner "
                "WHERE o.project_id=:p AND o.id=:child"
            ),
            {"p": project_id, "child": child_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["parent_profile_id"] != chief_profile_id(connection, project_id)
        or row["parent_session_mode"] == "team"
    ):
        return
    if row["status"] not in {"done", "needs_review", "failed", "unknown", "blocked"}:
        return
    intent_id = connection.scalar(
        text(
            "INSERT INTO chief_followup_intents "
            "(id,project_id,parent_work_order_id,child_work_order_id,child_version,"
            "child_status,source_decision_id) "
            "VALUES (:id,:p,:parent,:child,:version,:status,:decision) "
            "ON CONFLICT (project_id,child_work_order_id,child_version) DO NOTHING RETURNING id"
        ),
        {
            "id": uuid4(),
            "p": project_id,
            "parent": row["parent_work_order_id"],
            "child": child_id,
            "version": row["version"],
            "status": row["status"],
            "decision": row["decision_id"],
        },
    )
    if intent_id is not None:
        gateway._event(
            connection,
            project_id,
            row["parent_work_order_id"],
            None,
            "chief_followup_queued",
            {
                "intent_id": str(intent_id),
                "child_work_order_id": str(child_id),
                "child_status": row["status"],
            },
        )


def _finish(
    gateway: ToolGateway,
    intent: dict[str, Any],
    state: str,
    error: str | None,
    conversation_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> None:
    with gateway.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE chief_followup_intents SET state=:state,error_code=:error,"
                "conversation_id=:conversation,turn_id=:turn,lease_until=NULL,updated_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id AND state='leased'"
            ),
            {
                "state": state,
                "error": error,
                "conversation": conversation_id,
                "turn": turn_id,
                "p": intent["project_id"],
                "id": intent["id"],
            },
        )
        if changed.rowcount:
            gateway._event(
                connection,
                intent["project_id"],
                intent["parent_work_order_id"],
                None,
                f"chief_followup_{state}",
                {
                    "intent_id": str(intent["id"]),
                    "turn_id": str(turn_id) if turn_id else None,
                    "error_code": error,
                },
            )


def claim(gateway: ToolGateway, settings: Settings, project_id: UUID | None = None) -> dict[str, Any] | None:
    if settings.coordination_mode != "auto":
        return None
    with gateway.engine.begin() as connection:
        for expired in connection.execute(
            text(
                "UPDATE chief_followup_intents SET state='unknown',error_code='followup_interrupted',"
                "lease_until=NULL,updated_at=clock_timestamp() WHERE state='leased' "
                "AND lease_until<clock_timestamp() RETURNING id,project_id,parent_work_order_id"
            )
        ).mappings():
            gateway._event(
                connection,
                expired["project_id"],
                expired["parent_work_order_id"],
                None,
                "chief_followup_unknown",
                {"intent_id": str(expired["id"]), "error_code": "followup_interrupted"},
            )
        project_filter = "i.project_id=:project_id AND " if project_id is not None else ""
        intent = (
            connection.execute(
                text(
                    "SELECT i.id,i.project_id,i.parent_work_order_id,i.child_work_order_id,"  # noqa: S608
                    "i.child_version,i.child_status,i.source_decision_id,"
                    "d.turn_id,d.run_id,coalesce(source_chat.id,ac.id) AS conversation_id,"
                    "source_chat.is_active AS source_chat_active,o.status AS child_current_status,"
                    "o.version AS child_current_version,o.title AS child_title,"
                    "p.status AS parent_status,pr.status AS project_status,"
                    "owner.execution_mode AS parent_session_mode "
                    "FROM chief_followup_intents i "
                    "JOIN task_automation_decisions d ON d.project_id=i.project_id "
                    "AND d.id=i.source_decision_id "
                    "LEFT JOIN task_conversation_turns source_turn ON "
                    "source_turn.project_id=d.project_id AND source_turn.id=d.turn_id "
                    "LEFT JOIN task_conversations source_chat ON "
                    "source_chat.project_id=source_turn.project_id "
                    "AND source_chat.id=source_turn.conversation_id "
                    "AND source_chat.work_order_id=i.parent_work_order_id "
                    "LEFT JOIN task_conversations ac ON ac.project_id=i.project_id "
                    "AND ac.work_order_id=i.parent_work_order_id AND ac.is_active "
                    "AND d.turn_id IS NULL "
                    "JOIN work_orders o ON o.project_id=i.project_id AND o.id=i.child_work_order_id "
                    "JOIN work_orders p ON p.project_id=i.project_id AND p.id=i.parent_work_order_id "
                    "LEFT JOIN task_sessions owner ON owner.project_id=p.project_id "
                    "AND owner.work_order_id=p.id AND owner.is_order_owner "
                    "JOIN projects pr ON pr.id=i.project_id WHERE "
                    + project_filter
                    + "i.state='pending' AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                    "WHERE c.project_id=i.project_id AND c.paused) "
                    "ORDER BY i.created_at,i.id FOR UPDATE OF i SKIP LOCKED LIMIT 1"
                ),
                {"project_id": project_id} if project_id is not None else {},
            )
            .mappings()
            .one_or_none()
        )
        if intent is None:
            return None
        daily = int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM chief_followup_intents WHERE project_id=:p "
                    "AND state IN ('leased','done','unknown') "
                    "AND updated_at>=date_trunc('day',clock_timestamp())"
                ),
                {"p": intent["project_id"]},
            )
            or 0
        )
        error = (
            "team_session_active"
            if intent["parent_session_mode"] == "team"
            else "project_inactive"
            if intent["project_status"] != "active"
            else "parent_closed"
            if intent["parent_status"] in {"done", "failed"}
            else "source_conversation_inactive"
            if intent["turn_id"] is not None and intent["source_chat_active"] is not True
            else "child_changed"
            if intent["child_current_version"] != intent["child_version"]
            else "daily_limit"
            if daily >= settings.coordination_max_orders_per_day
            else None
        )
        connection.execute(
            text(
                "UPDATE chief_followup_intents SET state=:state,error_code=:error,attempts=attempts+1,"
                "lease_until=CASE WHEN :state='leased' THEN "
                "clock_timestamp()+(:seconds * interval '1 second') END,"
                "updated_at=clock_timestamp() WHERE project_id=:p AND id=:id"
            ),
            {
                "state": "blocked" if error else "leased",
                "error": error,
                "seconds": settings.live_text_timeout_seconds + 120,
                "p": intent["project_id"],
                "id": intent["id"],
            },
        )
        if error:
            gateway._event(
                connection,
                intent["project_id"],
                intent["parent_work_order_id"],
                None,
                "chief_followup_blocked",
                {"intent_id": str(intent["id"]), "error_code": error},
            )
            return {}
        return dict(intent)


def step(gateway: ToolGateway, settings: Settings, project_id: UUID | None = None) -> bool:
    """One model attempt for one child update, linked back to Chief's task."""
    from onefoundry.task_conversation_store import ConversationFailed, ConversationUnknown

    intent = claim(gateway, settings, project_id)
    if intent is None:
        return False
    if not intent:
        return True
    pid = intent["project_id"]
    parent = intent["parent_work_order_id"]
    conversation_id = intent["conversation_id"] or uuid5(NAMESPACE, f"{intent['id']}:conversation")
    opened_conversation: UUID | None = intent["conversation_id"]
    try:
        if intent["conversation_id"] is None:
            gateway.new_task_conversation(
                pid, parent, NewConversation(id=conversation_id, title="Chief follow-up")
            )
            opened_conversation = conversation_id
        with gateway.engine.connect() as connection:
            sequence = int(
                connection.scalar(
                    text(
                        "SELECT coalesce(max(sequence),0) FROM task_conversation_turns "
                        "WHERE project_id=:p AND conversation_id=:conversation"
                    ),
                    {"p": pid, "conversation": conversation_id},
                )
                or 0
            )
        message = (
            f"{intent['child_title']} is now {intent['child_status']}. "
            "Chief of Staff is reviewing the project next steps."
        )
        saved, _ = gateway.send_task_conversation(
            pid,
            parent,
            TurnSend(
                conversation_id=conversation_id,
                client_turn_id=uuid5(NAMESPACE, f"{intent['id']}:turn"),
                expected_sequence=sequence,
                text=message,
            ),
            source_kind="system_followup",
        )
        turn_id = UUID(str(saved["turn"]["id"]))
    except ConversationFailed:
        _finish(gateway, intent, "blocked", "model_failed", opened_conversation)
    except ConversationUnknown:
        _finish(gateway, intent, "unknown", "model_outcome_unknown", opened_conversation)
    except (Conflict, NotFound):
        _finish(gateway, intent, "blocked", "followup_unavailable", opened_conversation)
    except Exception:
        _finish(gateway, intent, "unknown", "followup_outcome_unknown", opened_conversation)
    else:
        _finish(gateway, intent, "done", None, conversation_id, turn_id)
    return True
