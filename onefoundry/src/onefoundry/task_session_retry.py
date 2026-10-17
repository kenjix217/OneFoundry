"""Chief-owned bounded recovery of a failed, unaccepted specialist assignment."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_admission import configured_profile, profile_blockers
from onefoundry.task_session_store import TeamLimits
from onefoundry.task_session_uncertainty import unresolved
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


class RetryAssignment(StrictModel):
    assignment_id: UUID
    direction: str = Field(min_length=1, max_length=4000)

    @field_validator("direction")
    @classmethod
    def safe_direction(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("retry direction contains sensitive text")
        return value


def retry(
    tools: "TaskSessionTools", scope: Mapping[str, Any], action_id: UUID, data: RetryAssignment
) -> dict[str, Any]:
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        if (
            scope["kind"] != "main"
            or scope["profile_id"] != chief_profile_id(connection, tools.project_id)
            or not scope["actionable"]
        ):
            raise Conflict("only the active canonical Chief may retry an assignment")
        row = (
            connection.execute(
                text(
                    "SELECT a.*,s.work_order_id,s.profile_id FROM task_session_assignments a "
                    "JOIN task_sessions s ON s.project_id=a.project_id AND "
                    "s.id=a.child_session_id "
                    "WHERE a.project_id=:p AND a.root_work_order_id=:root AND "
                    "a.parent_session_id=:parent AND a.id=:id FOR UPDATE OF a,s"
                ),
                {
                    "p": tools.project_id,
                    "root": scope["root_work_order_id"],
                    "parent": tools.session_id,
                    "id": data.assignment_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("assignment is outside this Chief's ownership")
        if row["status"] != "blocked" or row["result_message_id"] is not None:
            raise Conflict("retry requires a blocked assignment without an accepted result")
        if unresolved(connection, tools.project_id, root_id=scope["root_work_order_id"]):
            raise Conflict("reconcile the unknown outcome before retrying work")
        unsafe = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND session_id=:s "
                "AND status IN ('queued','running') "
                "UNION ALL SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND "
                "session_id=:s AND status='started' "
                "UNION ALL SELECT 1 FROM approval_requests WHERE project_id=:p AND "
                "work_order_id=:o AND status='pending' LIMIT 1"
            ),
            {"p": tools.project_id, "s": row["child_session_id"], "o": row["work_order_id"]},
        )
        rejected = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND work_order_id=:o AND "
                "event_type='task_session_qa_review' AND "
                "payload->>'verdict'='needs_rework' LIMIT 1"
            ),
            {"p": tools.project_id, "o": row["work_order_id"]},
        )
        if unsafe is not None or rejected is not None:
            raise Conflict(
                "QA rejection requires corrected inputs and fresh review; active or "
                "uncertain effects require reconciliation"
            )
        deps = connection.scalar(
            text(
                "SELECT 1 FROM task_session_dependencies d JOIN task_session_assignments a "
                "ON a.project_id=d.project_id AND a.id=d.depends_on_id "
                "WHERE d.project_id=:p AND d.assignment_id=:id AND a.status<>'satisfied' LIMIT 1"
            ),
            {"p": tools.project_id, "id": data.assignment_id},
        )
        if deps is not None:
            raise Conflict("retry inputs are not confirmed")
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id=:p AND "
                "event_type='task_session_assignment_retried' AND "
                "payload->>'assignment_id'=:id"
            ),
            {"p": tools.project_id, "id": str(data.assignment_id)},
        )
        if count >= TeamLimits.of(tools.gateway).chief_decisions_per_order:
            raise Conflict("configured Chief retry bound reached; explain the unresolved cause")
        settings = tools.gateway.live_text_settings
        assert settings is not None
        blockers = profile_blockers(
            configured_profile(connection, tools.project_id, row["profile_id"]), settings
        )
        if blockers:
            raise Conflict(" ".join(blockers))
        session = tools.sessions._by_id(
            connection, tools.project_id, scope["root_work_order_id"], row["child_session_id"]
        )
        message_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO task_session_messages "
                "(id,project_id,actor_id,root_work_order_id,session_id,source_session_id,sequence,kind,body) "
                "VALUES (:id,:p,:actor,:root,:s,:chief,:seq,'system',:body)"
            ),
            {
                "id": message_id,
                "p": tools.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "root": scope["root_work_order_id"],
                "s": session["id"],
                "chief": tools.session_id,
                "seq": tools.sessions._sequence(connection, tools.project_id, session["id"]) + 1,
                "body": "Chief recovery direction: "
                + data.direction
                + " Reuse confirmed work; inspect prior receipts before repeating an action.",
            },
        )
        connection.execute(
            text("UPDATE task_session_assignments SET status='ready' WHERE project_id=:p AND id=:id"),
            {"p": tools.project_id, "id": data.assignment_id},
        )
        connection.execute(
            text(
                "UPDATE work_orders SET "
                "status='ready',block_reason=NULL,blocked_from_status=NULL WHERE "
                "project_id=:p AND id=:o"
            ),
            {"p": tools.project_id, "o": row["work_order_id"]},
        )
        attempt_id = tools.sessions._queue_attempt(connection, session, source_message_id=message_id)
        result = {
            "assignment_id": str(data.assignment_id),
            "attempt_id": str(attempt_id),
            "session_id": str(session["id"]),
            "status": "queued",
            "tool_call_id": str(action_id),
        }
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "task_session_assignment_retried",
            result,
        )
        return result
