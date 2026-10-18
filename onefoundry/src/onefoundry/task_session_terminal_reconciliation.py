"""Confirm a saved terminal team effect without rewriting an uncertain provider turn."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_store import TaskSessionStore

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


EVENT = "task_session_terminal_attempt_reconciled"


def _artifact_matches(connection: Any, project_id: UUID, reference: Any) -> bool:
    if not isinstance(reference, dict):
        return False
    try:
        artifact_id = UUID(reference["artifact_id"])
        run_id = UUID(reference["run_id"])
        digest = reference["sha256"]
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        connection.scalar(
            text(
                "SELECT 1 FROM workspace_artifacts WHERE project_id=:p AND id=:id "
                "AND run_id=:run AND sha256=:digest AND status='ready'"
            ),
            {"p": project_id, "id": artifact_id, "run": run_id, "digest": digest},
        )
    )


def _qa_rework_matches(connection: Any, project_id: UUID, row: Any) -> bool:
    result = row["tool_result"]
    if not isinstance(result, dict) or result.get("status") != "needs_rework":
        return False
    saved = (
        connection.execute(
            text(
                "SELECT a.status,a.acceptance_kind,p.name,p.kind AS profile_kind,"
                "e.payload,m.body FROM task_session_assignments a "
                "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.child_session_id "
                "JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
                "JOIN events e ON e.project_id=a.project_id AND e.work_order_id=s.work_order_id "
                "AND e.event_type='task_session_qa_review' "
                "AND e.payload->>'tool_call_id'=:call "
                "JOIN task_session_messages m ON m.project_id=e.project_id "
                "AND m.id=(e.payload->>'message_id')::uuid "
                "WHERE a.project_id=:p AND a.child_session_id=:s AND m.session_id=:s LIMIT 1"
            ),
            {"p": project_id, "s": row["session_id"], "call": str(row["tool_call_id"])},
        )
        .mappings()
        .one_or_none()
    )
    if (
        saved is None
        or saved["status"] != "blocked"
        or saved["acceptance_kind"] != "qa_review"
        or saved["name"] != "QA"
        or saved["profile_kind"] != "builtin"
    ):
        return False
    event = saved["payload"]
    try:
        body = json.loads(saved["body"])
    except (TypeError, ValueError):
        return False
    references = event.get("reviewed_artifacts")
    return (
        isinstance(body, dict)
        and body.get("verdict") == event.get("verdict") == "needs_rework"
        and isinstance(references, list)
        and body.get("reviewed_artifacts") == references == result.get("reviewed_artifacts")
        and all(_artifact_matches(connection, project_id, item) for item in references)
        and result.get("message_id") == event.get("message_id")
    )


def _report_matches(connection: Any, project_id: UUID, row: Any) -> bool:
    if isinstance(row["tool_result"], dict) and row["tool_result"].get("status") == "needs_rework":
        return _qa_rework_matches(connection, project_id, row)
    assignment = (
        connection.execute(
            text(
                "SELECT a.id,a.status,a.acceptance_kind,a.result_message_id,a.result_sha256,m.body "
                "FROM task_session_assignments a JOIN task_session_messages m "
                "ON m.project_id=a.project_id AND m.id=a.result_message_id "
                "WHERE a.project_id=:p AND a.child_session_id=:s AND a.result_action_id=:call "
                "AND m.session_id=:s"
            ),
            {"p": project_id, "s": row["session_id"], "call": row["tool_call_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if assignment is None or assignment["status"] != "satisfied":
        return False
    if hashlib.sha256(assignment["body"].encode()).hexdigest() != assignment["result_sha256"]:
        return False
    result = row["tool_result"]
    if not isinstance(result, dict) or any(
        result.get(key) != value
        for key, value in (
            ("assignment_id", str(assignment["id"])),
            ("result_message_id", str(assignment["result_message_id"])),
            ("status", "saved"),
        )
    ):
        return False
    if assignment["acceptance_kind"] == "text_report":
        return True
    try:
        payload = json.loads(assignment["body"])
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if assignment["acceptance_kind"] == "artifact":
        return _artifact_matches(connection, project_id, payload.get("artifact"))
    if assignment["acceptance_kind"] == "qa_review":
        references = payload.get("reviewed_artifacts")
        return (
            isinstance(references, list)
            and references == result.get("reviewed_artifacts")
            and all(_artifact_matches(connection, project_id, item) for item in references)
            and payload.get("verdict") == "passed"
        )
    return False


def _completion_matches(connection: Any, project_id: UUID, row: Any) -> bool:
    receipt = (
        connection.execute(
            text(
                "SELECT e.payload,m.body,w.status FROM events e "
                "JOIN task_session_messages m ON m.project_id=e.project_id "
                "AND m.id=(e.payload->>'message_id')::uuid "
                "JOIN work_orders w ON w.project_id=e.project_id AND w.id=e.work_order_id "
                "WHERE e.project_id=:p AND e.work_order_id=:root "
                "AND e.event_type='task_session_completion_submitted' "
                "AND e.payload->>'tool_call_id'=:call LIMIT 1"
            ),
            {"p": project_id, "root": row["root_work_order_id"], "call": str(row["tool_call_id"])},
        )
        .mappings()
        .one_or_none()
    )
    if receipt is None or receipt["status"] not in ("done", "needs_review"):
        return False
    payload = receipt["payload"]
    result = row["tool_result"]
    return (
        isinstance(result, dict)
        and result.get("status") == payload.get("status")
        and result.get("message_id") == payload.get("message_id")
        and receipt["body"] == payload.get("summary")
    )


def confirmed_terminal_receipt(
    connection: Any, project_id: UUID, attempt_id: UUID, session_id: UUID, root_id: UUID
) -> UUID | None:
    """Return the exact last terminal receipt only when its saved owner effect matches."""
    receipts = (
        connection.execute(
            text(
                "SELECT c.id AS tool_call_id,c.tool_name,c.result AS tool_result,"
                "c.created_at AS call_created_at FROM task_session_tool_calls c "
                "WHERE c.project_id=:p AND c.attempt_id=:a AND c.session_id=:s "
                "AND c.root_work_order_id=:root AND c.status='done' "
                "AND c.tool_name IN ('onefoundry.report_result','onefoundry.submit_completion') "
                "ORDER BY c.created_at DESC,c.id DESC"
            ),
            {"p": project_id, "a": attempt_id, "s": session_id, "root": root_id},
        )
        .mappings()
        .all()
    )
    for receipt in receipts:
        row = {**receipt, "session_id": session_id, "root_work_order_id": root_id}
        later = connection.scalar(
            text(
                "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND attempt_id=:a "
                "AND id<>:call AND created_at>=:created LIMIT 1"
            ),
            {
                "p": project_id,
                "a": attempt_id,
                "call": receipt["tool_call_id"],
                "created": receipt["call_created_at"],
            },
        )
        if later is not None:
            continue
        valid = (
            _report_matches(connection, project_id, row)
            if receipt["tool_name"] == "onefoundry.report_result"
            else _completion_matches(connection, project_id, row)
        )
        if valid:
            return UUID(str(receipt["tool_call_id"]))
    return None


def reconcile(gateway: ToolGateway, project_id: UUID, order_id: UUID, session_id: UUID) -> int:
    """Append exact saved-effect proof for eligible unknown turns in one scoped session."""
    settings = gateway.live_text_settings
    if settings is None or gateway.actor_id != settings.operator_actor_id:
        raise Conflict("only the operator can reconcile terminal attempts")
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        scope = TaskSessionStore(gateway)._by_id(connection, project_id, order_id, session_id)
        candidates = (
            connection.execute(
                text(
                    "SELECT a.id,a.session_id,a.root_work_order_id,a.reason,c.id AS tool_call_id,"
                    "c.tool_name,c.result AS tool_result,c.created_at AS call_created_at "
                    "FROM task_session_attempts a JOIN task_session_tool_calls c "
                    "ON c.project_id=a.project_id AND c.attempt_id=a.id "
                    "WHERE a.project_id=:p AND a.session_id=:s AND a.status='unknown' "
                    "AND c.status='done' AND c.tool_name IN "
                    "('onefoundry.report_result','onefoundry.submit_completion') "
                    "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=a.project_id "
                    "AND e.work_order_id=a.root_work_order_id AND e.event_type IN "
                    "('task_session_unknown_acknowledged',:event) "
                    "AND e.payload->>'attempt_id'=a.id::text) "
                    "ORDER BY a.created_at,a.id FOR UPDATE OF a"
                ),
                {"p": project_id, "s": session_id, "event": EVENT},
            )
            .mappings()
            .all()
        )
        count = 0
        for row in candidates:
            busy = connection.scalar(
                text(
                    "SELECT 1 FROM task_session_attempts WHERE project_id=:p "
                    "AND root_work_order_id=:root AND status IN ('queued','running') LIMIT 1"
                ),
                {"p": project_id, "root": row["root_work_order_id"]},
            )
            started = connection.scalar(
                text(
                    "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p "
                    "AND root_work_order_id=:root AND status='started' LIMIT 1"
                ),
                {"p": project_id, "root": row["root_work_order_id"]},
            )
            pending = connection.scalar(
                text(
                    "WITH RECURSIVE descendants AS (SELECT id FROM work_orders "
                    "WHERE project_id=:p AND id=:root UNION ALL SELECT w.id FROM work_orders w "
                    "JOIN descendants d ON w.parent_work_order_id=d.id WHERE w.project_id=:p) "
                    "SELECT 1 FROM approval_requests a JOIN descendants d ON d.id=a.work_order_id "
                    "WHERE a.project_id=:p AND a.status='pending' LIMIT 1"
                ),
                {"p": project_id, "root": row["root_work_order_id"]},
            )
            if any(item is not None for item in (busy, started, pending)):
                continue
            confirmed = confirmed_terminal_receipt(
                connection, project_id, row["id"], session_id, row["root_work_order_id"]
            )
            if confirmed != row["tool_call_id"]:
                continue
            gateway._event(
                connection,
                project_id,
                scope["root_work_order_id"],
                None,
                EVENT,
                {
                    "attempt_id": str(row["id"]),
                    "session_id": str(session_id),
                    "terminal_tool_call_id": str(row["tool_call_id"]),
                    "terminal_tool": row["tool_name"],
                    "prior_status": "unknown",
                    "prior_reason": row["reason"],
                    "confirmed_effect": "saved_result"
                    if row["tool_name"] == "onefoundry.report_result"
                    else "submitted_completion",
                },
            )
            count += 1
        return count
