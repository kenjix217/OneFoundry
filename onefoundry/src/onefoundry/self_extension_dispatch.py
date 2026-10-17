"""Deferred coding dispatch preserves the existing one-running-run contract."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import RepositoryCodeLaunch
from onefoundry.repository_code_gateway import RepositoryManifestTooLarge
from onefoundry.self_extension_dispatch_rejection import reject

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def step(gateway: "ToolGateway") -> bool:
    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT e.project_id,e.payload FROM events e JOIN task_session_attempts a "
                    "ON a.project_id=e.project_id AND a.id=e.run_id JOIN task_sessions s "
                    "ON s.project_id=a.project_id AND s.id=a.session_id "
                    "WHERE e.event_type='self_extension_build_requested' AND a.status='done' "
                    "AND s.status<>'closed' "
                    "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id=e.project_id "
                    "AND r.work_order_id=e.work_order_id AND r.status='running') "
                    "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.project_id=e.project_id "
                    "AND done.event_type IN "
                    "('self_extension_build_dispatched','self_extension_dispatch_rejected') "
                    "AND done.payload->>'tool_call_id'=e.payload->>'tool_call_id') "
                    "ORDER BY e.created_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        if dispatch_intent(gateway, row):
            return True
    return False


def dispatch_intent(gateway: "ToolGateway", row: Any) -> bool:
    intent = row["payload"]
    try:
        detail = gateway._execution_gateway().launch_repository_code(
            row["project_id"],
            UUID(intent["work_order_id"]),
            RepositoryCodeLaunch.model_validate(intent["launch"]),
        )
    except (Conflict, Unavailable) as exc:
        reason = (
            "coding_manifest_too_large"
            if isinstance(exc, RepositoryManifestTooLarge)
            else "coding_dispatch_prerequisite_failed"
        )
        return reject(gateway, row["project_id"], intent, reason)
    with gateway.engine.begin() as connection:
        lock_project(connection, row["project_id"])
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p "
                "AND event_type='self_extension_build_dispatched' AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": row["project_id"], "id": intent["tool_call_id"]},
        )
        if prior is None:
            gateway._event(
                connection,
                row["project_id"],
                UUID(intent["work_order_id"]),
                detail["run"]["id"],
                "self_extension_build_dispatched",
                {
                    "tool_call_id": intent["tool_call_id"],
                    "run_id": str(detail["run"]["id"]),
                    "attempt_id": str(detail["attempts"][-1]["id"]),
                },
            )
    return True


def waiting(connection: Any, project_id: UUID, session_id: UUID) -> bool:
    return (
        connection.scalar(
            text(
                "SELECT 1 FROM events e WHERE e.project_id=:p "
                "AND ((e.event_type='self_extension_build_requested' AND e.payload->>'session_id'=:s) "
                "OR (e.event_type='self_extension_qa_requested' AND e.payload->>'qa_session_id'=:s)) "
                "AND NOT EXISTS (SELECT 1 FROM events rejected WHERE rejected.project_id=e.project_id "
                "AND rejected.event_type='self_extension_dispatch_rejected' "
                "AND rejected.payload->>'tool_call_id'=e.payload->>'tool_call_id') "
                "AND EXISTS (SELECT 1 FROM task_session_assignments a WHERE a.project_id=e.project_id "
                "AND a.child_session_id::text=:s AND a.status IN ('ready','active')) LIMIT 1"
            ),
            {"p": project_id, "s": str(session_id)},
        )
        is not None
    )
