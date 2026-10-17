"""Public saved agent sessions, including closed sessions and failed tool receipts."""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.chief_inspection_pages import InspectionPageArgs, anchor_page, inspection_page, record_page
from onefoundry.gateway_errors import NotFound


class SessionInspectionArgs(InspectionPageArgs):
    session_id: UUID
    section: Literal["messages", "attempts", "actions"] = "messages"


QUERIES = {
    "messages": "SELECT id,sequence,actor_id,kind,body,created_at FROM task_session_messages "
    "WHERE project_id=:p AND session_id=:target AND created_at<:created_before ORDER BY sequence",
    "attempts": "SELECT id,status,provider,model_id,provider_thread_id,provider_turn_id,"
    "input_sequence,prompt_sha256,context_omitted_count,input_tokens,output_tokens,cost_usd,reason,"
    "retry_of_attempt_id,not_before,created_at,started_at,finished_at FROM task_session_attempts "
    "WHERE project_id=:p AND session_id=:target AND created_at<:created_before ORDER BY created_at,id",
    "actions": "SELECT id,attempt_id,tool_name,status,result,reason,created_at,finished_at "
    "FROM task_session_tool_calls WHERE project_id=:p AND session_id=:target "
    "AND created_at<:created_before ORDER BY created_at,id",
}


def session_records(
    connection: Connection,
    project_id: UUID,
    arguments: SessionInspectionArgs,
    max_items: int,
    byte_budget: int,
) -> dict[str, Any]:
    arguments = anchor_page(connection, arguments)
    session = (
        connection.execute(
            text(
                "SELECT s.id,s.work_order_id,s.root_work_order_id,s.parent_session_id,s.profile_id,"
                "p.name AS agent,s.kind,s.status,s.execution_mode,w.status AS work_order_status "
                "FROM task_sessions s JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
                "JOIN work_orders w ON w.project_id=s.project_id AND w.id=s.work_order_id "
                "WHERE s.project_id=:p AND s.id=:target"
            ),
            {"p": project_id, "target": arguments.session_id},
        )
        .mappings()
        .one_or_none()
    )
    if session is None:
        raise NotFound("session not found in this project")
    rows, next_offset = record_page(
        connection,
        QUERIES[arguments.section],
        {"p": project_id, "target": arguments.session_id},
        arguments,
        max_items,
    )
    return inspection_page(
        {"session": dict(session), "section": arguments.section, "records": rows},
        arguments,
        byte_budget,
        next_offset,
    )
