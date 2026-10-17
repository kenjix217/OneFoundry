"""Evaluate saved deliverables before a run may complete its work order.

Run success is an execution fact. This owner checks separate, project-scoped
records before recording a job outcome. Free-form acceptance checks require an
operator decision; a model's assertion never satisfies them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry import coordination_store
from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def evidence(connection: Any, project_id: UUID, order_id: UUID, run_id: UUID | None) -> dict[str, Any]:
    order = (
        connection.execute(
            text(
                "SELECT deliverable_kind,acceptance_checks,status FROM work_orders "
                "WHERE project_id=:p AND id=:o"
            ),
            {"p": project_id, "o": order_id},
        )
        .mappings()
        .one_or_none()
    )
    if order is None:
        raise NotFound("work order not found")
    kind = order["deliverable_kind"]
    found: list[dict[str, str]] = []
    if kind == "text_reply" and run_id is not None:
        for table, digest in (("live_text_outputs", "text_sha256"), ("text_outputs", "text_sha256")):
            row = (
                connection.execute(
                    text(
                        f"SELECT id,{digest} AS sha256 FROM {table} "  # noqa: S608 - fixed table names
                        "WHERE project_id=:p AND run_id=:r ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"p": project_id, "r": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                found.append({"source": table, "id": str(row["id"]), "sha256": row["sha256"]})
    elif kind in {"artifact", "document"} and run_id is not None:
        rows = connection.execute(
            text(
                "SELECT id,relative_path,sha256,kind FROM workspace_artifacts "
                "WHERE project_id=:p AND run_id=:r AND status='ready' "
                "AND relative_path<>'' AND sha256 ~ '^[0-9a-f]{64}$' "
                "ORDER BY created_at,id LIMIT 8"
            ),
            {"p": project_id, "r": run_id},
        ).mappings()
        for row in rows:
            if kind == "artifact" or row["kind"] in {"text", "markdown", "csv"}:
                found.append(
                    {
                        "source": "workspace_artifacts",
                        "id": str(row["id"]),
                        "path": row["relative_path"],
                        "sha256": row["sha256"],
                    }
                )
    elif kind == "code_change" and run_id is not None:
        qa = (
            connection.execute(
                text(
                    "SELECT q.id,q.receipt_sha256,d.id AS artifact_id,d.path,d.sha256 "
                    "FROM runner_jobs q JOIN runs r ON r.project_id=q.project_id "
                    "AND r.id=q.run_id AND r.mode='synthetic_code' "
                    "JOIN runner_jobs engineer ON "
                    "engineer.project_id=q.project_id AND engineer.run_id=q.run_id "
                    "AND engineer.id=q.source_job_id AND engineer.stage='engineer' "
                    "AND engineer.status='done' "
                    "JOIN runner_artifacts d ON d.project_id=engineer.project_id "
                    "AND d.job_id=engineer.id AND d.kind='diff' "
                    "WHERE q.project_id=:p AND q.run_id=:r AND q.stage='qa' AND q.status='done' "
                    "AND q.receipt_sha256 ~ '^[0-9a-f]{64}$' "
                    'AND q.tests @> \'[{"status":"passed"}]\'::jsonb LIMIT 1'
                ),
                {"p": project_id, "r": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if qa is not None:
            found.append(
                {
                    "source": "runner_jobs.qa",
                    "id": str(qa["id"]),
                    "receipt_sha256": qa["receipt_sha256"],
                    "path": qa["path"],
                    "sha256": qa["sha256"],
                }
            )
    elif kind == "external_action":
        outbound = (
            connection.execute(
                text(
                    "SELECT id,remote_message_id FROM remote_outbound "
                    "WHERE project_id=:p AND work_order_id=:o AND status='accepted_by_platform' "
                    "AND remote_message_id IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if outbound is not None:
            found.append(
                {
                    "source": "remote_outbound",
                    "id": str(outbound["id"]),
                    "readback_id": outbound["remote_message_id"],
                }
            )
        if run_id is not None:
            sale = (
                connection.execute(
                    text(
                        "SELECT s.id FROM sales_drafts s JOIN approval_requests a ON "
                        "a.project_id=s.project_id AND a.id=s.approval_id "
                        "WHERE s.project_id=:p AND s.run_id=:r AND s.delivery_status='delivered' "
                        "AND s.sent_at IS NOT NULL AND a.status='approved' LIMIT 1"
                    ),
                    {"p": project_id, "r": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if sale is not None:
                found.append({"source": "sales_drafts", "id": str(sale["id"])})
    if not found and kind in {"artifact", "document"}:
        submitted = (
            connection.execute(
                text(
                    "SELECT x.id,x.relative_path,x.sha256 FROM events e JOIN workspace_artifacts x "
                    "ON x.project_id=e.project_id AND x.id::text=e.payload->'artifact'->>'artifact_id' "
                    "AND x.sha256=e.payload->'artifact'->>'sha256' WHERE e.project_id=:p "
                    "AND e.work_order_id=:o AND e.event_type='task_session_completion_submitted' "
                    "AND x.status='ready' ORDER BY e.created_at DESC LIMIT 1"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if submitted is not None:
            found.append(
                {
                    "source": "workspace_artifacts",
                    "id": str(submitted["id"]),
                    "path": submitted["relative_path"],
                    "sha256": submitted["sha256"],
                }
            )
    accepted = bool(
        order["status"] == "done"
        and connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND work_order_id=:o "
                "AND event_type='work_order_accepted' ORDER BY created_at DESC,id DESC LIMIT 1"
            ),
            {"p": project_id, "o": order_id},
        )
    )
    checks = list(order["acceptance_checks"])
    missing = [] if found or accepted else [f"{kind} evidence"]
    if not accepted:
        missing.extend(checks)
    return {"kind": kind, "evidence": found, "operator_accepted": accepted, "missing_checks": missing}


def settle(
    gateway: ToolGateway,
    connection: Any,
    project_id: UUID,
    order_id: UUID,
    run_id: UUID,
    run_outcome: str,
) -> str:
    """Change one job state after an already saved run outcome, with an event."""
    order = (
        connection.execute(
            text("SELECT status FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
            {"p": project_id, "o": order_id},
        )
        .mappings()
        .one_or_none()
    )
    if order is None:
        raise NotFound("work order not found")
    if order["status"] == "blocked":
        gateway._event(
            connection,
            project_id,
            order_id,
            run_id,
            "work_order_completion_deferred",
            {"reason": "blocked", "run_outcome": run_outcome},
        )
        return "blocked"
    if order["status"] not in {"running", "ready"}:
        raise Conflict("work order is no longer awaiting this run")
    matched = evidence(connection, project_id, order_id, run_id)
    status = (
        run_outcome
        if run_outcome in {"failed", "unknown"}
        else "done"
        if matched["evidence"] and not matched["missing_checks"]
        else "needs_review"
    )
    connection.execute(
        text("UPDATE work_orders SET status=:s WHERE project_id=:p AND id=:o"),
        {"s": status, "p": project_id, "o": order_id},
    )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "work_order_completion_evaluated",
        {
            "run_outcome": run_outcome,
            "status": status,
            "deliverable_kind": matched["kind"],
            "evidence": matched["evidence"],
            "missing_checks": matched["missing_checks"],
        },
    )
    coordination_store.emit_work_order_signal(gateway, connection, project_id, order_id, status, run_id)
    return status


def external_action_result(
    gateway: ToolGateway,
    connection: Any,
    project_id: UUID,
    order_id: UUID,
    run_id: UUID | None,
    *,
    confirmed: bool,
) -> str:
    """Reconcile an approved outbound action from its owner readback."""
    order = (
        connection.execute(
            text("SELECT status,deliverable_kind FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
            {"p": project_id, "o": order_id},
        )
        .mappings()
        .one_or_none()
    )
    if order is None:
        raise NotFound("work order not found")
    prior = str(order["status"])
    if order["deliverable_kind"] != "external_action":
        return prior
    if prior not in {"needs_review", "unknown"}:
        gateway._event(
            connection,
            project_id,
            order_id,
            run_id,
            "work_order_external_action_observed",
            {"confirmed": confirmed, "status": prior},
        )
        return prior
    matched = evidence(connection, project_id, order_id, run_id)
    status = (
        "unknown"
        if not confirmed
        else "done"
        if matched["evidence"] and not matched["missing_checks"]
        else "needs_review"
    )
    if status != prior:
        connection.execute(
            text("UPDATE work_orders SET status=:s WHERE project_id=:p AND id=:o"),
            {"s": status, "p": project_id, "o": order_id},
        )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "work_order_external_action_observed",
        {
            "confirmed": confirmed,
            "status": status,
            "evidence": matched["evidence"],
            "missing_checks": matched["missing_checks"],
        },
    )
    if status != prior:
        coordination_store.emit_work_order_signal(gateway, connection, project_id, order_id, status, run_id)
    return status
