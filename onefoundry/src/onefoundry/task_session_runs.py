"""Bind a team attempt to existing run-owned tools and artifact receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry import run_audit
from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def bind(gateway: ToolGateway, project_id: UUID, claimed: Any) -> None:
    """The attempt ID is the run ID; one durable identity prevents duplicate runs."""
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        row = (
            connection.execute(
                text(
                    "SELECT w.profile_id,w.status,a.purpose,s.kind FROM work_orders w JOIN task_sessions s "
                    "ON s.project_id=w.project_id AND s.work_order_id=w.id JOIN task_session_attempts a "
                    "ON a.project_id=s.project_id AND a.session_id=s.id WHERE a.project_id=:p AND a.id=:a "
                    "AND a.status='running' FOR UPDATE OF w"
                ),
                {"p": project_id, "a": claimed["id"]},
            )
            .mappings()
            .one()
        )
        if row["profile_id"] != claimed["profile_id"]:
            raise Conflict("historical child session needs an operator migration before role tools can run")
        if row["purpose"] == "discussion":
            from onefoundry.agent_conversation_policy import allows_main_discussion

            if row["kind"] != "main" or not allows_main_discussion(
                connection, project_id, claimed["root_work_order_id"], row["profile_id"]
            ):
                raise Conflict("discussion run requires an admitted main conversation")
        elif row["status"] not in ("ready", "running"):
            raise Conflict("work order is not active")
        connection.execute(
            text(
                "UPDATE work_orders SET status='running' "
                "WHERE project_id=:p AND id=:o AND status='ready' AND :work"
            ),
            {"p": project_id, "o": claimed["work_order_id"], "work": row["purpose"] == "work"},
        )
        if (
            connection.scalar(
                text("SELECT 1 FROM runs WHERE project_id=:p AND id=:a"),
                {"p": project_id, "a": claimed["id"]},
            )
            is not None
        ):
            raise Conflict("this team attempt already owns a run; reconcile it before continuing")
        run_audit.insert_run(
            connection,
            project_id,
            claimed["id"],
            claimed["work_order_id"],
            claimed["profile_id"],
            f"profile:{claimed['profile_id']}",
            "live_text",
            extension_worker_image=gateway.live_text_settings.extension_worker_image
            if gateway.live_text_settings
            else None,
        )
        gateway._event(
            connection,
            project_id,
            claimed["work_order_id"],
            claimed["id"],
            "team_run_bound",
            {"attempt_id": str(claimed["id"]), "session_id": str(claimed["session_id"])},
        )


def settle(
    connection: Any,
    project_id: UUID,
    attempt_id: UUID,
    outcome: str,
    reason: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    used = None if input_tokens is None or output_tokens is None else input_tokens + output_tokens
    connection.execute(
        text(
            "UPDATE runs SET status=:status,outcome_reason=:reason,tokens_used=:used, "
            "finished_at=clock_timestamp() "
            "WHERE project_id=:p AND id=:a AND status='running'"
        ),
        {
            "p": project_id,
            "a": attempt_id,
            "status": outcome,
            "reason": reason or "team_turn_finished",
            "used": used,
        },
    )
