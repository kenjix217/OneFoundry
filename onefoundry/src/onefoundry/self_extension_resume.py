"""Confirmed activation consumes one original checkpoint through existing follow-up."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.capability_schemas import CapabilityReport
from onefoundry.extension_activation_contract import ExtensionActivationJob, ExtensionActivationReceipt
from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project
from onefoundry.self_extension_continuation import wake
from onefoundry.task_execution_capabilities import execution_capabilities

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def step(gateway: ToolGateway) -> bool:
    settings = gateway.live_text_settings
    if settings is None:
        return False
    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT project_id,payload FROM events e WHERE event_type='self_extensi"
                    "on_activation_queued' "
                    "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.project_id=e.proj"
                    "ect_id AND done.event_type='self_extension_activation_observed' "
                    "AND done.payload->>'approval_id'=e.payload->>'approval_id') ORDER BY c"
                    "reated_at,id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        job = ExtensionActivationJob.model_validate(row["payload"]["manifest"])
        observed = gateway._execution_gateway().spool.read_receipt(job.job_id)
        if observed is None:
            continue
        receipt = ExtensionActivationReceipt.model_validate(observed[0])
        if (
            receipt.job_id != job.job_id
            or receipt.project_id != job.project_id
            or receipt.proposal_digest != job.proposal_digest
        ):
            raise Conflict("activation receipt identity changed")
        if receipt.status == "activated" and (
            receipt.installed_version != job.release.version
            or receipt.installed_image != job.release.app_image
            or not receipt.capability_ready
            or not receipt.checkpoint_saved
        ):
            raise Conflict("activation receipt lacks installed owner readback")
        with gateway.engine.begin() as connection:
            lock_project(connection, job.project_id)
            prior = connection.scalar(
                text(
                    "SELECT 1 FROM events WHERE project_id=:p "
                    "AND event_type='self_extension_activation_observed' "
                    "AND payload->>'approval_id'=:id LIMIT 1"
                ),
                {"p": job.project_id, "id": str(job.approval_id)},
            )
            if prior is not None:
                continue
            gateway._event(
                connection,
                job.project_id,
                job.original_work_order_id,
                None,
                "self_extension_activation_observed",
                {
                    "approval_id": str(job.approval_id),
                    "receipt_sha256": observed[1],
                    "receipt": receipt.model_dump(mode="json"),
                    "manifest": job.model_dump(mode="json"),
                },
            )
        return True
    with gateway.engine.connect() as connection:
        candidates = (
            connection.execute(
                text(
                    "SELECT e.project_id,e.payload,request.payload AS checkpoint FROM events e "
                    "JOIN events request ON request.project_id=e.project_id AND request.eve"
                    "nt_type='self_extension_requested' "
                    "AND request.payload->>'target_approval_id'=e.payload->'manifest'->>'target_approval_id' "
                    "WHERE e.event_type='self_extension_activation_observed' AND e.payload-"
                    ">'receipt'->>'status'='activated' "
                    "AND NOT EXISTS (SELECT 1 FROM events resumed WHERE resumed.project_id="
                    "e.project_id AND resumed.event_type='self_extension_resumed' "
                    "AND resumed.payload->>'approval_id'=e.payload->>'approval_id' AND resu"
                    "med.payload->>'original_session_id'=request.payload->>'original_sessio"
                    "n_id') "
                    "ORDER BY e.created_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for pending in candidates:
        if resume_pending(gateway, pending):
            return True
    return False


def resume_pending(gateway: ToolGateway, pending: Any) -> bool:
    settings = gateway.live_text_settings
    if settings is None:
        return False
    checkpoint = pending["checkpoint"]
    session_id = UUID(checkpoint["original_session_id"])
    with gateway.engine.connect() as connection:
        profile_id = connection.scalar(
            text("SELECT profile_id FROM task_sessions WHERE project_id=:p AND id=:s"),
            {"p": pending["project_id"], "s": session_id},
        )
        if profile_id is None:
            return False
        report = CapabilityReport.model_validate(
            execution_capabilities(
                connection, pending["project_id"], settings, profile_id=profile_id, gateway=gateway
            )["readiness"]
        )
    checks = list(report.checks)
    if checkpoint["operation"].startswith("tool:"):
        from onefoundry.requested_capability import inspect

        checks.append(inspect(gateway, pending["project_id"], profile_id, checkpoint["operation"][5:]))
    if not any(
        check.operation == checkpoint["operation"]
        and (str(check.source_id) if check.source_id else None) == checkpoint["gap_source_id"]
        and check.status == "ready"
        for check in checks
    ):
        return False
    approval_id = UUID(pending["payload"]["approval_id"])

    def record(connection: Any) -> None:
        gateway._event(
            connection,
            pending["project_id"],
            UUID(checkpoint["original_work_order_id"]),
            None,
            "self_extension_resumed",
            {
                "approval_id": str(approval_id),
                "original_session_id": str(session_id),
                "extension_key": checkpoint["extension_key"],
                "checkpoint": checkpoint["checkpoint"],
            },
        )

    return wake(
        gateway,
        pending["project_id"],
        session_id,
        approval_id,
        "The extension is installed and passed capability readback. Read extens"
        "ion status to restore the original checkpoint and verify the requested"
        " result. Company knowledge follows source review and publication.",
        on_queued=record,
    )


def require_resumed(connection: Any, project_id: UUID, root_id: UUID) -> None:
    from onefoundry.self_extension_completion import require_resumed as verify_completion

    verify_completion(connection, project_id, root_id)
