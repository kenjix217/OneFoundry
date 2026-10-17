"""Explicit operator return of one completed child, preserving its prior evidence."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.task_session_uncertainty import unresolved as has_uncertainty
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

EVENT = "task_session_assignment_returned"


class ChildReworkCreate(StrictModel):
    client_action_id: UUID
    expected_sequence: int = Field(ge=0)
    expected_result_message_id: UUID
    body: str = Field(min_length=10, max_length=MAX_OPERATOR_MESSAGE_CHARS)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        value = local_text(value)
        if len(value) < 10 or contains_sensitive_text(value):
            raise ValueError("revision must include safe instructions without sensitive text")
        return value


class ChildReworkOut(StrictModel):
    assignment_id: UUID
    session_id: UUID
    message_id: UUID
    attempt_id: UUID
    sequence: int
    prior_result_message_id: UUID


def _scope(connection: Any, project_id: UUID, session: dict[str, Any]) -> dict[str, Any]:
    if session["kind"] != "child" or session["execution_mode"] != "team" or not session["is_order_owner"]:
        raise Conflict("return for rework requires a completed assigned child")
    row = (
        connection.execute(
            text(
                "SELECT a.id,a.status,a.result_message_id,a.result_action_id,a.result_sha256,"
                "a.plan_revision,a.acceptance_kind,"
                "w.status AS order_status,root.status AS root_status,"
                "m.status AS parent_status,m.id AS parent_id "
                "FROM task_session_assignments a JOIN work_orders w "
                "ON w.project_id=a.project_id AND w.id=:order JOIN work_orders root "
                "ON root.project_id=a.project_id AND root.id=a.root_work_order_id "
                "JOIN task_sessions m ON m.project_id=a.project_id AND m.id=a.parent_session_id "
                "AND m.kind='main' AND m.is_order_owner AND m.execution_mode='team' "
                "WHERE a.project_id=:p AND a.child_session_id=:s FOR UPDATE OF a,w,root,m"
            ),
            {"p": project_id, "s": session["id"], "order": session["work_order_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise Conflict("nested or unassigned work needs a new Chief plan rather than an in-place return")
    return dict(row)


def _safe_to_return(connection: Any, project_id: UUID, session: dict[str, Any], assignment_id: UUID) -> None:
    root = session["root_work_order_id"]
    busy = connection.scalar(
        text(
            "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND root_work_order_id=:root "
            "AND status IN ('queued','running') LIMIT 1"
        ),
        {"p": project_id, "root": root},
    )
    unfinished = connection.scalar(
        text(
            "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND root_work_order_id=:root "
            "AND status='started' LIMIT 1"
        ),
        {"p": project_id, "root": root},
    )
    pending = connection.scalar(
        text(
            "SELECT 1 FROM approval_requests a JOIN task_sessions s ON s.project_id=a.project_id "
            "AND s.work_order_id=a.work_order_id WHERE a.project_id=:p AND s.root_work_order_id=:root "
            "AND a.status='pending' LIMIT 1"
        ),
        {"p": project_id, "root": root},
    )
    if busy or unfinished or pending or has_uncertainty(connection, project_id, root_id=root):
        raise Conflict("wait for execution and reconcile unknown actions/approvals before returning work")
    consumed = connection.scalar(
        text(
            "WITH RECURSIVE consumers(id) AS (SELECT assignment_id FROM task_session_dependencies "
            "WHERE project_id=:p AND depends_on_id=:a UNION SELECT d.assignment_id "
            "FROM task_session_dependencies d JOIN consumers c ON d.depends_on_id=c.id "
            "WHERE d.project_id=:p) SELECT 1 FROM task_session_assignments a JOIN consumers c ON c.id=a.id "
            "WHERE a.project_id=:p AND a.status NOT IN ('pending','canceled') LIMIT 1"
        ),
        {"p": project_id, "a": assignment_id},
    )
    descendants = connection.scalar(
        text(
            "SELECT 1 FROM task_session_assignments WHERE project_id=:p AND parent_session_id=:s "
            "AND status<>'canceled' LIMIT 1"
        ),
        {"p": project_id, "s": session["id"]},
    )
    if consumed or descendants:
        raise Conflict("dependent work already used this result; ask the Chief for a new bounded plan")
    missing = connection.scalar(
        text(
            "SELECT 1 FROM task_session_dependencies d JOIN task_session_assignments a "
            "ON a.project_id=d.project_id AND a.id=d.depends_on_id WHERE d.project_id=:p "
            "AND d.assignment_id=:a AND a.status<>'satisfied' LIMIT 1"
        ),
        {"p": project_id, "a": assignment_id},
    )
    if missing:
        raise Conflict("the child's required input is no longer confirmed")


def return_child(
    gateway: ToolGateway, project_id: UUID, order_id: UUID, session_id: UUID, data: ChildReworkCreate
) -> tuple[dict[str, Any], bool]:
    settings = gateway.live_text_settings
    if settings is None or gateway.actor_id != settings.operator_actor_id:
        raise Conflict("only the operator can return an assigned child for rework")
    digest = hashlib.sha256(json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    sessions = TaskSessionStore(gateway)
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        session = sessions._by_id(connection, project_id, order_id, session_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type=:event "
                "AND payload->>'client_action_id'=:client LIMIT 1"
            ),
            {"p": project_id, "event": EVENT, "client": str(data.client_action_id)},
        )
        if prior is not None:
            if prior["request_sha256"] != digest or prior["result"]["session_id"] != str(session_id):
                raise Conflict("return identity was already used for a different revision")
            return dict(prior["result"]), False
        context_limit = settings.onefoundry_max_context_bytes
        if context_limit is not None and len(data.body.encode("utf-8")) > context_limit:
            raise Conflict("operator message exceeds configured context budget")
        if not settings.team_worker_enabled:
            raise Conflict("team worker is disabled; no revision turn was queued")
        scope = _scope(connection, project_id, session)
        if (
            session["status"] != "closed"
            or scope["status"] != "satisfied"
            or scope["order_status"] != "needs_review"
            or scope["acceptance_kind"] not in ("artifact", "text_report")
            or scope["root_status"] not in ("ready", "running", "needs_review")
            or scope["parent_status"] == "closed"
        ):
            raise Conflict("return requires an unaccepted completed child in an open team")
        if scope["result_message_id"] != data.expected_result_message_id:
            raise Conflict("the saved result changed; refresh before returning it")
        sequence = sessions._sequence(connection, project_id, session_id)
        if sequence != data.expected_sequence:
            raise Conflict("session sequence changed; refresh before returning work")
        _safe_to_return(connection, project_id, session, scope["id"])
        message_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO task_session_messages (id,project_id,actor_id,root_work_order_id,session_id,"
                "client_message_id,sequence,kind,body) VALUES (:id,:p,:actor,:root,:s,:client,:seq,"
                "'operator',:body)"
            ),
            {
                "id": message_id,
                "p": project_id,
                "actor": gateway.actor_id,
                "root": session["root_work_order_id"],
                "s": session_id,
                "client": data.client_action_id,
                "seq": sequence + 1,
                "body": data.body,
            },
        )
        # Assignment fields describe the current accepted input. Its previous
        # identities remain in immutable messages, tool receipts and this return event.
        connection.execute(
            text(
                "UPDATE task_session_assignments SET status='ready',result_message_id=NULL,"
                "result_action_id=NULL,result_sha256=NULL WHERE project_id=:p AND id=:a"
            ),
            {"p": project_id, "a": scope["id"]},
        )
        connection.execute(
            text("UPDATE work_orders SET status='ready' WHERE project_id=:p AND id=:o"),
            {"p": project_id, "o": session["work_order_id"]},
        )
        connection.execute(
            text("UPDATE work_orders SET status='running' WHERE project_id=:p AND id=:o"),
            {"p": project_id, "o": session["root_work_order_id"]},
        )
        parent_sequence = sessions._sequence(connection, project_id, scope["parent_id"]) + 1
        connection.execute(
            text(
                "INSERT INTO task_session_messages (id,project_id,actor_id,root_work_order_id,session_id,"
                "sequence,kind,body) VALUES (:id,:p,:actor,:root,:s,:seq,'operator',:body)"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": gateway.actor_id,
                "root": session["root_work_order_id"],
                "s": scope["parent_id"],
                "seq": parent_sequence,
                "body": f"Operator returned assignment {scope['id']} for rework. Result "
                f"{data.expected_result_message_id} is historical evidence, not the current "
                "accepted input. Wait for its revised confirmed result before completion.",
            },
        )
        connection.execute(
            text("UPDATE task_sessions SET status='open' WHERE project_id=:p AND id=:s"),
            {"p": project_id, "s": scope["parent_id"]},
        )
        attempt_id = sessions._queue_attempt(connection, session, source_message_id=message_id)
        result = {
            "assignment_id": str(scope["id"]),
            "session_id": str(session_id),
            "message_id": str(message_id),
            "attempt_id": str(attempt_id),
            "sequence": sequence + 1,
            "prior_result_message_id": str(data.expected_result_message_id),
        }
        gateway._event(
            connection,
            project_id,
            session["root_work_order_id"],
            None,
            EVENT,
            {
                "client_action_id": str(data.client_action_id),
                "request_sha256": digest,
                "result": result,
                "plan_revision": scope["plan_revision"],
                "prior_result": {
                    "message_id": str(scope["result_message_id"]),
                    "action_id": str(scope["result_action_id"]),
                    "body_sha256": scope["result_sha256"],
                },
            },
        )
        return result, True


def prior_result_receipt(
    connection: Any, project_id: UUID, child_session_id: UUID, action_id: UUID, digest: str
) -> dict[str, Any] | None:
    """An old result retry reads its immutable receipt without satisfying new work."""
    prior = connection.scalar(
        text(
            "SELECT payload FROM events WHERE project_id=:p AND event_type=:event "
            "AND payload->'prior_result'->>'action_id'=:action LIMIT 1"
        ),
        {"p": project_id, "event": EVENT, "action": str(action_id)},
    )
    if prior is None:
        return None
    if (
        prior["result"]["session_id"] != str(child_session_id)
        or prior["prior_result"]["body_sha256"] != digest
    ):
        raise Conflict("result action identity was used for different work")
    return {
        "assignment_id": UUID(prior["result"]["assignment_id"]),
        "result_message_id": UUID(prior["prior_result"]["message_id"]),
        "released": [],
    }
