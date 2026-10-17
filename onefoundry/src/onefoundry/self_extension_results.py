"""Exact coding and QA evidence through existing assignment/result owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry import task_session_results
from onefoundry.extension_qa_contract import ExtensionQaJob, ExtensionQaReceipt
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest
from onefoundry.repository_code_observation import saved_manifest
from onefoundry.runner_spool import SpoolInvalid
from onefoundry.task_session_report_contract import ReportArgs

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


def _coding(
    tools: TaskSessionTools, scope: dict[str, Any], run_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    execution = tools.gateway._execution_gateway()
    with tools.gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.work_order_id,s.id AS child_session_id FROM runs r JOIN task_sessions s "
                    "ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                    "JOIN events e ON e.project_id=s.project_id AND e.event_type='self_extension_requested' "
                    "AND e.payload->>'child_session_id'=s.id::text "
                    "WHERE r.project_id=:p AND r.id=:r AND (s.id=:s OR "
                    "e.payload->>'original_work_order_id'=:root) LIMIT 1"
                ),
                {
                    "p": tools.project_id,
                    "r": run_id,
                    "s": tools.session_id,
                    "root": str(scope["root_work_order_id"]),
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("coding evidence is outside this extension task")
    detail = execution.reconcile(tools.project_id, run_id)
    attempt = detail["attempts"][-1]
    if attempt["fixture_id"] != "repository_code_v1":
        raise Conflict("fixture evidence cannot satisfy a maintenance extension")
    if attempt["status"] != "done":
        raise Conflict("coding is not a confirmed candidate; wait or reconcile the unknown outcome")
    return detail, dict(row)


def finish_build(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, run_id: UUID
) -> dict[str, Any]:
    _, linkage = _coding(tools, scope, run_id)
    if linkage["child_session_id"] != tools.session_id:
        raise Conflict("only the assigned Engineer may report its coding result")
    body = (
        "The coding candidate passed the configured checks. "
        "Independent QA and operator-approved activation are still required."
    )
    saved = tools.assignments.report_text_result(
        tools.project_id, tools.session_id, tools.attempt_id, action_id, body, evidence_kind="operator_review"
    )
    return {
        "assignment_id": str(saved["assignment_id"]),
        "result_message_id": str(saved["result_message_id"]),
        "released": [str(item) for item in saved["released"]],
    }


def launch_qa(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, run_id: UUID
) -> dict[str, Any]:
    detail, linkage = _coding(tools, scope, run_id)
    if linkage["child_session_id"] == tools.session_id:
        raise Conflict("Engineer cannot independently verify its own candidate")
    with tools.gateway.engine.connect() as connection:
        dependency = connection.scalar(
            text(
                "SELECT 1 FROM task_session_assignments qa JOIN task_session_dependencies d O"
                "N d.project_id=qa.project_id AND d.assignment_id=qa.id "
                "JOIN task_session_assignments engineer ON engineer.project_id=d.project_id A"
                "ND engineer.id=d.depends_on_id "
                "WHERE qa.project_id=:p AND qa.child_session_id=:s AND qa.acceptance_kind='qa_review' "
                "AND engineer.child_session_id=:engineer AND engineer.status='satisfied' LIMIT 1"
            ),
            {"p": tools.project_id, "s": tools.session_id, "engineer": linkage["child_session_id"]},
        )
        if dependency is None:
            raise Conflict("independent QA must declare the exact confirmed Engineer dependency")
    attempt = detail["attempts"][-1]
    execution = tools.gateway._execution_gateway()
    job = saved_manifest(execution, tools.project_id, run_id)
    diff = next(item for item in attempt["artifacts"] if item["kind"] == "diff")
    unsigned = {
        "schema_version": 1,
        "kind": "extension_qa_v1",
        "job_id": str(action_id),
        "engineer_job": job.model_dump(mode="json"),
        "baseline_commit": attempt["baseline_commit"],
        "candidate_commit": attempt["commit"],
        "diff_sha256": diff["sha256"],
        "diff_byte_size": diff["byte_size"],
    }
    payload = ExtensionQaJob.model_validate({**unsigned, "request_sha256": digest(unsigned)}).model_dump(
        mode="json"
    )
    import json

    if len(json.dumps(payload).encode()) > 8192:
        raise Conflict("independent QA manifest exceeds the bounded runner transport")
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='self_extensio"
                "n_qa_requested' AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            return {"qa_job_id": str(action_id), "status": "queued"}
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            None,
            "self_extension_qa_requested",
            {
                "tool_call_id": str(action_id),
                "qa_session_id": str(tools.session_id),
                "qa_profile_id": str(scope["profile_id"]),
                "coding_run_id": str(run_id),
                "manifest": payload,
            },
        )
    try:
        from runner.repository_transport import dispatch_job

        dispatch_job(execution.spool, action_id, payload)
    except (OSError, SpoolInvalid):
        # Dispatch identity is durable and cannot be automatically recreated.
        raise Conflict("QA dispatch is uncertain; reconcile before another request") from None
    return {
        "qa_job_id": str(action_id),
        "status": "queued",
        "next_step": "Finish this turn; read the independent QA receipt on a later turn.",
    }


def qa_result(tools: TaskSessionTools, job_id: UUID) -> tuple[ExtensionQaReceipt, str]:
    with tools.gateway.engine.connect() as connection:
        value = connection.scalar(
            text(
                "SELECT payload->'manifest' FROM events WHERE project_id=:p "
                "AND event_type='self_extension_qa_requested' AND payload->>'tool_call_id'=:job "
                "AND payload->>'qa_session_id'=:session LIMIT 1"
            ),
            {"p": tools.project_id, "job": str(job_id), "session": str(tools.session_id)},
        )
    if value is None:
        raise NotFound("independent QA job not found for this session")
    job = ExtensionQaJob.model_validate(value)
    observed = tools.gateway._execution_gateway().spool.read_receipt(job_id)
    if observed is None:
        raise Conflict("independent QA has no receipt yet")
    receipt = ExtensionQaReceipt.model_validate(observed[0])
    if (
        receipt.job_id != job_id
        or receipt.project_id != tools.project_id
        or receipt.request_sha256 != job.request_sha256
        or receipt.candidate_commit != job.candidate_commit
        or receipt.diff_sha256 != job.diff_sha256
        or receipt.test_image != job.engineer_job.target.test_image
        or (
            receipt.status == "passed"
            and [check.name for check in receipt.tests]
            != [f"qa_{i}" for i in range(len(job.engineer_job.target.test_commands))]
        )
    ):
        raise Conflict("independent QA receipt differs from the saved candidate")
    return receipt, observed[1]


def finish_qa(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, job_id: UUID
) -> dict[str, Any]:
    receipt, sha = qa_result(tools, job_id)
    if receipt.status == "unknown":
        raise Conflict("QA outcome is unknown; it cannot authorize activation")
    body = (
        f"Independent isolated QA {receipt.status}. "
        "The review applies to the saved candidate and its exact configured checks."
    )
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_qa_v"
                "erified' AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is None:
            tools.gateway._event(
                connection,
                tools.project_id,
                scope["work_order_id"],
                None,
                "self_extension_qa_verified",
                {
                    "tool_call_id": str(action_id),
                    "qa_job_id": str(job_id),
                    "qa_session_id": str(tools.session_id),
                    "qa_profile_id": str(scope["profile_id"]),
                    "receipt_sha256": sha,
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
    return task_session_results.report(
        tools,
        scope,
        action_id,
        ReportArgs(body=body, verdict="passed" if receipt.status == "passed" else "needs_rework"),
    )
