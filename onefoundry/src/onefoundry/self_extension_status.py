"""Readable progress from persisted owners; never infer success from agent text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.extension_activation_contract import ExtensionActivationReceipt
from onefoundry.extension_qa_contract import ExtensionQaReceipt
from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.runner_spool import SpoolInvalid

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

MESSAGES = {
    "needs_setup": (
        "Coding needs setup",
        "The assigned work is saved. Check the maintenance scope, "
        "model, provider and runner readiness before coding can start.",
    ),
    "waiting_coding": (
        "Engineer assigned",
        "The original task is saved while Engineer prepares the missing capability.",
    ),
    "coding": (
        "Engineer is building",
        "The separate coding runner is working on the approved disposable source.",
    ),
    "coding_failed": (
        "Coding needs attention",
        "The coding attempt failed. Review the saved outcome before another attempt.",
    ),
    "coding_abandoned": (
        "Unverified build discarded",
        "The operator discarded this stopped attempt. Its outcome remains unknown; "
        "a replacement requires a new approved scope.",
    ),
    "waiting_qa": (
        "Independent QA needed",
        "A candidate is saved. Independent QA must review the exact change.",
    ),
    "qa": (
        "QA is checking the candidate",
        "QA reconstructs the saved change and runs the approved checks independently.",
    ),
    "qa_failed": ("QA needs changes", "The candidate did not pass independent QA. It cannot be activated."),
    "waiting_activation": (
        "Candidate verified",
        "QA passed. A verified release package and activation proposal are still required.",
    ),
    "approval_pending": (
        "Activation needs your approval",
        "Review the exact release, configuration effects and rollback plan in the inbox.",
    ),
    "activation_rejected": (
        "Activation rejected",
        "The original task remains paused. The rejected release has not been activated.",
    ),
    "activating": (
        "Activation queued",
        "The release supervisor waits for idle work, then verifies and applies the approved package.",
    ),
    "activation_failed": (
        "Activation needs attention",
        "Activation failed. Inspect the observed installed release and recovery outcome.",
    ),
    "waiting_resume": (
        "Checking the original capability",
        "Activation was observed. The task resumes after current permissions and capability pass readback.",
    ),
    "resumed": (
        "Original task resumed",
        "The original checkpoint was restored once. Chief still needs to verify the requested result.",
    ),
    "unknown": (
        "Outcome needs reconciliation",
        "The outcome is uncertain. Automatic replay and completion are blocked.",
    ),
}


def decorate(gateway: ToolGateway, connection: Any, project_id: UUID, item: dict[str, Any]) -> None:
    request = item["payload"]
    stage = {
        None: "waiting_coding",
        "queued": "coding",
        "done": "waiting_qa",
        "failed": "coding_failed",
        "unknown": "unknown",
    }[item["coding_status"]]
    if item["coding_status"] == "unknown":
        attempt_id = UUID(item["coding_attempt_id"])
        observed = gateway._execution_gateway().spool.read_receipt(attempt_id)
        if observed is not None:
            receipt_code = ExternalCodeReceipt.model_validate(observed[0])
            if receipt_code.project_id != project_id or receipt_code.attempt_id != attempt_id:
                raise SpoolInvalid("extension coding status identity changed")
            abandoned = connection.scalar(
                text(
                    "SELECT 1 FROM events WHERE project_id=:p AND event_type='repository_code_abandoned' "
                    "AND payload->>'attempt_id'=:a AND payload->>'request_sha256'=:request "
                    "AND payload->>'receipt_sha256'=:receipt LIMIT 1"
                ),
                {
                    "p": project_id,
                    "a": str(attempt_id),
                    "request": receipt_code.request_sha256,
                    "receipt": observed[1],
                },
            )
            if abandoned is not None:
                stage = "coding_abandoned"
            elif (
                receipt_code.status == "unknown"
                and receipt_code.commit is None
                and not receipt_code.artifacts
            ):
                item["abandonment"] = {
                    "attempt_id": str(attempt_id),
                    "run_id": str(receipt_code.run_id),
                    "request_sha256": receipt_code.request_sha256,
                    "receipt_sha256": observed[1],
                }
    if item["coding_status"] is None:
        blocked = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND work_order_id=:order "
                "AND event_type='self_extension_dispatch_blocked' LIMIT 1"
            ),
            {"p": project_id, "order": item["work_order_id"]},
        )
        if blocked is not None:
            stage = "needs_setup"
    if item["qa_jobs"]:
        job_id = UUID(item["qa_jobs"][0]["qa_job_id"])
        observed = gateway._execution_gateway().spool.read_receipt(job_id)
        stage = "qa"
        if observed is not None:
            receipt = ExtensionQaReceipt.model_validate(observed[0])
            if receipt.project_id != project_id or receipt.job_id != job_id:
                raise SpoolInvalid("extension QA status identity changed")
            stage = {"passed": "waiting_qa", "failed": "qa_failed", "unknown": "unknown"}[receipt.status]
            verified = connection.scalar(
                text(
                    "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_qa_verified' "
                    "AND payload->>'qa_job_id'=:job LIMIT 1"
                ),
                {"p": project_id, "job": str(job_id)},
            )
            if receipt.status == "passed" and verified is not None:
                stage = "waiting_activation"
    activation = (
        connection.execute(
            text(
                "SELECT e.payload,a.id,a.status FROM events e JOIN approval_requests a "
                "ON a.project_id=e.project_id AND a.id::text=e.payload->>'approval_id' "
                "WHERE e.project_id=:p AND e.event_type='self_extension_activation_proposed' "
                "AND e.payload->>'candidate_commit'=:commit ORDER BY e.created_at DESC,e.id DESC LIMIT 1"
            ),
            {"p": project_id, "commit": item["commit"]},
        )
        .mappings()
        .one_or_none()
    )
    if activation is not None:
        stage = {"pending": "approval_pending", "rejected": "activation_rejected", "approved": "activating"}[
            activation["status"]
        ]
        value = connection.scalar(
            text(
                "SELECT payload->'receipt' FROM events WHERE project_id=:p "
                "AND event_type='self_extension_activation_observed' AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"p": project_id, "id": str(activation["id"])},
        )
        if value is not None:
            receipt_activation = ExtensionActivationReceipt.model_validate(value)
            stage = {"activated": "waiting_resume", "failed": "activation_failed", "unknown": "unknown"}[
                receipt_activation.status
            ]
    resumed = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_resumed' "
            "AND payload->>'extension_key'=:key AND payload->>'original_session_id'=:session LIMIT 1"
        ),
        {"p": project_id, "key": request["extension_key"], "session": request["original_session_id"]},
    )
    if resumed is not None:
        stage = "resumed"
    item.update(
        {
            "extension_key": request["extension_key"],
            "stage": stage,
            "title": MESSAGES[stage][0],
            "message": MESSAGES[stage][1],
        }
    )
