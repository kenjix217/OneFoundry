"""Receipt-driven wakeups through the existing task message/attempt owner."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.task_session_schemas import SessionMessageCreate
from onefoundry.task_session_store import TaskSessionStore

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def wake(
    gateway: ToolGateway,
    project_id: UUID,
    session_id: UUID,
    cause_id: UUID,
    body: str,
    *,
    on_queued: Callable[[Any], None] | None = None,
    still_applicable: Callable[[Any], bool] | None = None,
) -> bool:
    """One terminal cause creates one message; active/unknown work is never replayed."""
    with gateway.engine.begin() as connection:
        from onefoundry.profile_hierarchy import lock_project
        from onefoundry.task_session_uncertainty import unresolved

        lock_project(connection, project_id)
        if still_applicable is not None and not still_applicable(connection):
            return False
        session = (
            connection.execute(
                text("SELECT * FROM task_sessions WHERE project_id=:p AND id=:s FOR UPDATE"),
                {"p": project_id, "s": session_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            session is None
            or session["status"] == "closed"
            or unresolved(connection, project_id, session_id=session_id)
        ):
            return False
        busy = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND session_id=:s AN"
                "D status IN ('queued','running') LIMIT 1"
            ),
            {"p": project_id, "s": session_id},
        )
        if busy is not None:
            return False
        message_id = uuid5(NAMESPACE_URL, f"onefoundry:extension-cause:{session_id}:{cause_id}")
        if (
            connection.scalar(
                text("SELECT 1 FROM task_session_messages WHERE project_id=:p AND client_message_id=:m"),
                {"p": project_id, "m": message_id},
            )
            is not None
        ):
            return False
        sequence = TaskSessionStore._sequence(connection, project_id, session_id)
        try:
            TaskSessionStore(gateway).send_in_transaction(
                connection,
                project_id,
                session["work_order_id"],
                SessionMessageCreate(client_message_id=message_id, expected_sequence=sequence, body=body),
                session_id=session_id,
            )
        except Conflict:
            return False
        gateway._event(
            connection,
            project_id,
            session["work_order_id"],
            None,
            "self_extension_followup_queued",
            {"cause_id": str(cause_id), "session_id": str(session_id), "message_id": str(message_id)},
        )
        if on_queued is not None:
            on_queued(connection)
        return True


def step(gateway: ToolGateway) -> bool:
    """Inspect one persisted coding/QA dispatch, without running a CLI in the app."""
    from onefoundry.self_extension_target_resume import step as resume_target

    if resume_target(gateway):
        return True
    try:
        execution = gateway._execution_gateway()
    except Unavailable:
        return False
    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT a.project_id,a.id,a.run_id,s.id AS session_id FROM external_code_attempts a "
                    "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id JOIN task_sessions s "
                    "ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                    "WHERE a.fixture_id='repository_code_v1' AND (a.status IN ('queued','done','failed') "
                    "OR (a.status='unknown' AND EXISTS (SELECT 1 FROM events abandoned "
                    "WHERE abandoned.project_id=a.project_id AND "
                    "abandoned.event_type='repository_code_abandoned' "
                    "AND abandoned.payload->>'attempt_id'=a.id::text "
                    "AND abandoned.payload->>'request_sha256'=a.request_sha256 "
                    "AND abandoned.payload->>'receipt_sha256'=a.receipt_sha256))) AN"
                    "D s.status<>'closed' "
                    "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=a.project_id "
                    "AND e.event_type IN ('self_extension_followup_queued',"
                    "'self_extension_coding_failure_observed','self_extension_coding_abandonment_observed') "
                    "AND e.payload->>'cause_id'"
                    "=a.id::text) "
                    "ORDER BY a.created_at,a.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        detail = execution.reconcile(row["project_id"], row["run_id"])
        if detail["attempts"][-1]["status"] in ("failed", "unknown"):
            from onefoundry.task_session_assignments import TaskSessionAssignments

            if TaskSessionAssignments(gateway).block_failed_coding(row["project_id"], row["id"]):
                return True
        if detail["attempts"][-1]["status"] == "done":
            if wake(
                gateway,
                row["project_id"],
                row["session_id"],
                row["id"],
                "The extension coding candidate is saved. Read extension status, report the e"
                "xact coding receipt, and finish this turn so independent QA can continue.",
            ):
                return True
    with gateway.engine.connect() as connection:
        qa_rows = (
            connection.execute(
                text(
                    "SELECT e.project_id,e.payload FROM events e JOIN task_sessions s ON s.projec"
                    "t_id=e.project_id "
                    "AND s.id::text=e.payload->>'qa_session_id' WHERE event_type='self_extension_"
                    "qa_requested' AND s.status<>'closed' "
                    "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.project_id=e.project_id "
                    "AND done.event_type='self_extension_followup_queued' "
                    "AND done.payload->>'cause_id'=e.payload->>'tool_call_id') ORDER BY e.created"
                    "_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for qa in qa_rows:
        job_id = UUID(qa["payload"]["tool_call_id"])
        observed = execution.spool.read_receipt(job_id)
        if observed is not None:
            if wake(
                gateway,
                qa["project_id"],
                UUID(qa["payload"]["qa_session_id"]),
                job_id,
                "Independent extension QA has returned a receipt. Read extension status and s"
                "ubmit the observed review; unknown outcomes require reconciliation.",
            ):
                return True
    return False


def status(gateway: ToolGateway, project_id: UUID, root_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        if (
            connection.scalar(
                text("SELECT 1 FROM work_orders WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": root_id},
            )
            is None
        ):
            raise NotFound("work order not found")
        rows = (
            connection.execute(
                text(
                    "SELECT e.payload,s.work_order_id,a.id AS coding_attempt_id,a.run_id AS coding_run_id,"
                    "a.status AS coding_status,a.commit,a.diff_sha256 FROM events e JOIN task_sessions s "
                    "ON s.project_id=e.project_id AND s.id::text=e.payload->>'child_session_id' "
                    "LEFT JOIN runs r ON r.project_id=s.project_id AND r.work_order_id=s.work_ord"
                    "er_id AND r.mode='external_code' "
                    "LEFT JOIN external_code_attempts a ON a.project_id=r.project_id AND a.run_id"
                    "=r.id AND a.fixture_id='repository_code_v1' "
                    "WHERE e.project_id=:p AND e.event_type='self_extension_requested' "
                    "AND e.payload->>'original_work_order_id'=:root ORDER BY e.created_at,e.id LIMIT 50"
                ),
                {"p": project_id, "root": str(root_id)},
            )
            .mappings()
            .all()
        )
        result = [
            {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}
            for row in rows
        ]
        for item in result:
            qa: Any = (
                connection.execute(
                    text(
                        "SELECT payload FROM events WHERE project_id=:p AND event_type='self_extensio"
                        "n_qa_requested' AND payload->>'coding_run_id'=:run ORDER BY created_at DESC,"
                        "id DESC LIMIT 8"
                    ),
                    {"p": project_id, "run": str(item["coding_run_id"])},
                )
                .scalars()
                .all()
            )
            item["qa_jobs"] = [
                {"qa_job_id": value["tool_call_id"], "qa_session_id": value["qa_session_id"]} for value in qa
            ]
            from onefoundry.self_extension_status import decorate

            decorate(gateway, connection, project_id, item)
        return result
