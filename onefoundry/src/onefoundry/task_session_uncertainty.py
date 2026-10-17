"""Operator acknowledgement of an uncertain turn without rewriting its history."""

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class UnknownAcknowledgement(StrictModel):
    client_action_id: UUID
    decision: Literal["abandon_unverified"]
    reason: str = Field(min_length=10, max_length=1000)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("acknowledgement reason contains sensitive text")
        return value


def unresolved(
    connection: Any, project_id: UUID, *, root_id: UUID | None = None, session_id: UUID | None = None
) -> bool:
    if (root_id is None) == (session_id is None):
        raise ValueError("exactly one uncertainty scope is required")
    external = connection.scalar(
        text(
            "SELECT 1 FROM external_code_attempts a JOIN runs r "
            "ON r.project_id=a.project_id AND r.id=a.run_id "
            "JOIN task_sessions s ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
            "WHERE a.project_id=:p AND a.fixture_id='repository_code_v1' AND a.status='unknown' "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=a.project_id "
            "AND e.event_type='repository_code_abandoned' AND e.payload->>'attempt_id'=a.id::text "
            "AND e.payload->>'request_sha256'=a.request_sha256 "
            "AND e.payload->>'receipt_sha256'=a.receipt_sha256) "
            "AND (CAST(:root AS uuid) IS NULL OR s.root_work_order_id=:root) "
            "AND (CAST(:session AS uuid) IS NULL OR s.id=:session) LIMIT 1"
        ),
        {"p": project_id, "root": root_id, "session": session_id},
    )
    if external is not None:
        return True
    return (
        connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts a WHERE a.project_id=:p AND a.status='unknown' "
                "AND (CAST(:root AS uuid) IS NULL OR a.root_work_order_id=:root) "
                "AND (CAST(:session AS uuid) IS NULL OR a.session_id=:session) AND NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.project_id=a.project_id "
                "AND e.work_order_id=a.root_work_order_id "
                "AND e.event_type='task_session_unknown_acknowledged' "
                "AND e.payload->>'attempt_id'=a.id::text) LIMIT 1"
            ),
            {"p": project_id, "root": root_id, "session": session_id},
        )
        is not None
    )


def acknowledge(
    gateway: "ToolGateway",
    project_id: UUID,
    order_id: UUID,
    session_id: UUID,
    attempt_id: UUID,
    data: UnknownAcknowledgement,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None or gateway.actor_id != settings.operator_actor_id:
        raise Conflict("only the operator can acknowledge an unknown outcome")
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        scope = TaskSessionStore(gateway)._by_id(connection, project_id, order_id, session_id)
        attempt = (
            connection.execute(
                text(
                    "SELECT status,root_work_order_id FROM task_session_attempts "
                    "WHERE project_id=:p AND session_id=:s AND id=:a"
                ),
                {"p": project_id, "s": session_id, "a": attempt_id},
            )
            .mappings()
            .one_or_none()
        )
        if attempt is None:
            raise NotFound("attempt not found in this session")
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND work_order_id=:root "
                "AND event_type='task_session_unknown_acknowledged' AND (payload->>'attempt_id'=:a "
                "OR payload->>'client_action_id'=:client) LIMIT 1"
            ),
            {
                "p": project_id,
                "root": scope["root_work_order_id"],
                "a": str(attempt_id),
                "client": str(data.client_action_id),
            },
        )
        result = {
            "attempt_id": str(attempt_id),
            "client_action_id": str(data.client_action_id),
            "decision": data.decision,
            "reason": data.reason,
            "status": "unknown",
        }
        if prior is not None:
            if dict(prior) != result:
                raise Conflict("this unknown outcome already has a different acknowledgement")
            return result
        if attempt["status"] != "unknown":
            raise Conflict("only an unknown outcome can be acknowledged")
        busy = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND root_work_order_id=:root "
                "AND status IN ('queued','running') LIMIT 1"
            ),
            {"p": project_id, "root": scope["root_work_order_id"]},
        )
        receipt = connection.scalar(
            text(
                "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND attempt_id=:a "
                "AND status='started' LIMIT 1"
            ),
            {"p": project_id, "a": attempt_id},
        )
        pending = connection.scalar(
            text(
                "SELECT 1 FROM approval_requests a JOIN work_orders w ON w.project_id=a.project_id "
                "AND w.id=a.work_order_id WHERE a.project_id=:p AND a.status='pending' "
                "AND (w.id=:root OR w.parent_work_order_id=:root) LIMIT 1"
            ),
            {"p": project_id, "root": scope["root_work_order_id"]},
        )
        if busy is not None or receipt is not None or pending is not None:
            raise Conflict(
                "wait for execution and reconcile saved actions/approvals before acknowledging uncertainty"
            )
        sequence = TaskSessionStore._sequence(connection, project_id, session_id) + 1
        connection.execute(
            text(
                "INSERT INTO task_session_messages (id,project_id,actor_id,root_work_order_id,session_id,"
                "sequence,kind,body) VALUES (:id,:p,:actor,:root,:s,:seq,'operator',:body)"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": gateway.actor_id,
                "root": scope["root_work_order_id"],
                "s": session_id,
                "seq": sequence,
                "body": f"Operator abandoned the unverified outcome of attempt {attempt_id}: {data.reason}. "
                "Its history remains unknown. Do not replay its actions or use an unverified result. "
                "Continue only with confirmed evidence and a separate attempt/revision.",
            },
        )
        gateway._event(
            connection,
            project_id,
            scope["root_work_order_id"],
            None,
            "task_session_unknown_acknowledged",
            result,
        )
        return result
