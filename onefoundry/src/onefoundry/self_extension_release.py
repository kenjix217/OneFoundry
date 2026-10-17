"""Chief-owned packaging requests, durable dispatch and original-task continuation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text

from onefoundry.extension_qa_contract import ExtensionQaJob, ExtensionQaReceipt
from onefoundry.extension_release_preparation_contract import (
    PrepareRelease,
    ReleasePreparationJob,
    ReleasePreparationReceipt,
)
from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest
from onefoundry.self_extension_continuation import wake
from onefoundry.self_extension_results import _coding

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools
    from onefoundry.tool_gateway import ToolGateway


def request(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, data: PrepareRelease
) -> dict[str, Any]:
    detail, _ = _coding(tools, scope, data.coding_run_id)
    candidate = detail["attempts"][-1]
    gateway = tools.gateway
    spool = gateway._execution_gateway().spool
    with gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        verified = connection.scalar(
            text(
                "SELECT e.payload FROM events e JOIN task_session_assignments a ON a.project_id=e.project_id "
                "AND a.child_session_id::text=e.payload->>'qa_session_id' "
                "JOIN profiles p ON p.project_id=e.project_id "
                "AND p.id::text=e.payload->>'qa_profile_id' WHERE e.project_id=:p "
                "AND e.event_type='self_extension_qa_verified' AND e.payload->>'qa_job_id'=:job "
                "AND a.status='satisfied' AND p.kind='builtin' AND p.name='QA' "
                "AND a.root_work_order_id=:root "
                "ORDER BY e.created_at DESC LIMIT 1"
            ),
            {"p": tools.project_id, "job": str(data.qa_job_id), "root": scope["root_work_order_id"]},
        )
        if verified is None:
            raise Conflict("packaging needs confirmed independent QA for this task")
        review = ExtensionQaReceipt.model_validate(verified["receipt"])
        if (
            review.status != "passed"
            or review.candidate_commit != candidate["commit"]
            or review.diff_sha256 != candidate["diff_sha256"]
        ):
            raise Conflict("QA does not accept this exact candidate")
        qa = ExtensionQaJob.model_validate_json(
            spool._read_regular(spool.root / "jobs" / f"{data.qa_job_id}.json", 8192)
        )
        if (
            qa.engineer_job.project_id != tools.project_id
            or qa.engineer_job.run_id != data.coding_run_id
            or qa.request_sha256 != review.request_sha256
        ):
            raise Conflict("packaging QA job identity changed")
        job_id = uuid5(
            NAMESPACE_URL,
            "onefoundry:release-prepare:"
            + digest(
                {
                    "session": str(tools.session_id),
                    "qa": verified["receipt_sha256"],
                    "request": data.model_dump(mode="json"),
                }
            ),
        )
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='self_extension_release_requested' AND payload->>'job_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(job_id)},
        )
        if prior is not None:
            return dict(prior)
        from onefoundry.extension_release_capability import inspect as release_capability

        if release_capability(gateway)["status"] != "ready":
            raise Unavailable("Connect the separate release worker before preparing this package.")
        value = {
            "job_id": str(job_id),
            "project_id": str(tools.project_id),
            "session_id": str(tools.session_id),
            "work_order_id": str(scope["root_work_order_id"]),
            "qa_job": qa.model_dump(mode="json"),
            "qa_receipt_sha256": verified["receipt_sha256"],
            "request": data.model_dump(mode="json"),
            "kind": "extension_release_prepare_v1",
            "schema_version": 1,
        }
        job = ReleasePreparationJob.model_validate({**value, "request_sha256": digest(value)})
        payload = {
            "tool_call_id": str(action_id),
            "job_id": str(job_id),
            "job": job.model_dump(mode="json"),
            "status": "queued",
        }
        gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "self_extension_release_requested",
            payload,
        )
        return payload


def _requests(gateway: ToolGateway) -> list[Any]:
    with gateway.engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT e.project_id,e.payload FROM events e "
                    "WHERE e.event_type='self_extension_release_requested' AND NOT EXISTS "
                    "(SELECT 1 FROM events done WHERE done.project_id=e.project_id "
                    "AND done.event_type='self_extension_release_observed' "
                    "AND done.payload->>'job_id'=e.payload->>'job_id') ORDER BY e.created_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )


def step(gateway: ToolGateway) -> bool:
    try:
        spool = gateway._execution_gateway().spool
    except Unavailable:
        return False
    for row in _requests(gateway):
        job = ReleasePreparationJob.model_validate(row["payload"]["job"])
        # Idempotent create verifies exact bytes after a crash between dispatch
        # and observation. A claimed uncertain build is never relaunched here.
        with gateway.engine.begin() as connection:
            lock_project(connection, job.project_id)
            path = spool.root / "jobs" / f"{job.job_id}.json"
            if path.exists():
                if ReleasePreparationJob.model_validate_json(spool._read_regular(path, 32768)) != job:
                    raise Conflict("packaging dispatch identity changed")
            else:
                spool.write_job(job.job_id, job.model_dump(mode="json"), group_readable=True, max_bytes=32768)
        observed = spool.read_receipt(job.job_id)
        if observed is None:
            continue
        receipt = ReleasePreparationReceipt.model_validate(observed[0])
        if (
            receipt.job_id != job.job_id
            or receipt.project_id != job.project_id
            or receipt.request_sha256 != job.request_sha256
        ):
            raise Conflict("packaging receipt differs from the requested candidate")

        def record(
            connection: Any, saved: ReleasePreparationReceipt = receipt, work: UUID = job.work_order_id
        ) -> None:
            gateway._event(
                connection,
                saved.project_id,
                work,
                None,
                "self_extension_release_observed",
                {"job_id": str(saved.job_id), "receipt": saved.model_dump(mode="json")},
            )

        if wake(
            gateway,
            job.project_id,
            job.session_id,
            job.job_id,
            f"Release packaging {receipt.status}. Read extension status for the exact release record. "
            "Only a prepared package can be proposed for installation approval. "
            "Continue the original checkpoint after verified activation.",
            on_queued=record,
        ):
            return True
    return False


def status(gateway: ToolGateway, project_id: UUID, work_order_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        rows: list[Any] = list(
            connection.execute(
                text(
                    "SELECT payload FROM events WHERE project_id=:p AND work_order_id=:w "
                    "AND event_type='self_extension_release_requested' ORDER BY created_at,id LIMIT 50"
                ),
                {"p": project_id, "w": work_order_id},
            )
            .scalars()
            .all()
        )
    if not rows:
        return []
    spool = gateway._execution_gateway().spool
    output = []
    for value in rows:
        job = ReleasePreparationJob.model_validate(value["job"])
        saved = spool.read_receipt(job.job_id)
        receipt = ReleasePreparationReceipt.model_validate(saved[0]) if saved is not None else None
        if receipt is not None and (
            receipt.project_id != project_id
            or receipt.job_id != job.job_id
            or receipt.request_sha256 != job.request_sha256
        ):
            raise Conflict("packaging receipt identity changed")
        output.append(
            {
                "job_id": str(job.job_id),
                "coding_run_id": str(job.request.coding_run_id),
                "status": receipt.status if receipt else "pending",
                "receipt": receipt.model_dump(mode="json") if receipt else None,
            }
        )
    return output
