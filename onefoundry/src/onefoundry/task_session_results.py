"""Verify team artifacts, QA verdicts and objective evidence through their owners."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import text

from onefoundry import chief_verified_completion
from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_assignments import TaskSessionAssignments
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_report_contract import ReportArgs as ReportArgs
from onefoundry.task_session_uncertainty import unresolved
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


class CompletionArgs(StrictModel):
    body: str = Field(min_length=1, max_length=12000)
    artifact_id: UUID | None = None
    artifact_run_id: UUID | None = None


def _artifact(
    tools: TaskSessionTools, scope: Mapping[str, Any], run_id: UUID, artifact_id: UUID, *, own: bool
) -> dict[str, Any]:
    with tools.gateway.engine.connect() as connection:
        valid = connection.scalar(
            text(
                "SELECT 1 FROM task_sessions s JOIN task_session_attempts a "
                "ON a.project_id=s.project_id AND a.session_id=s.id JOIN runs r "
                "ON r.project_id=a.project_id AND r.id=a.id WHERE s.project_id=:p "
                "AND s.root_work_order_id=:root AND a.id=:r "
                "AND (NOT :own OR s.id=:session)"
            ),
            {
                "p": tools.project_id,
                "root": scope["root_work_order_id"],
                "r": run_id,
                "own": own,
                "session": tools.session_id,
            },
        )
        if valid is None:
            raise Conflict("artifact is outside this agent's team evidence scope")
    preview = tools.gateway.workspace_artifact(tools.project_id, run_id, artifact_id)
    result = {
        "artifact_id": str(artifact_id),
        "run_id": str(run_id),
        "sha256": preview["sha256"],
        "kind": preview["kind"],
        "content": preview["content"],
    }
    if preview["kind"] == "video":
        result["playback_verified"] = preview.get("playback_verified", False)
    return result


def report(
    tools: TaskSessionTools, scope: Mapping[str, Any], action_id: UUID, data: ReportArgs
) -> dict[str, Any]:
    body = local_text(data.body)
    if contains_sensitive_text(body):
        raise ValueError("sensitive result")
    kind = scope["assignment"]["acceptance_kind"]
    artifacts: list[dict[str, Any]] = []
    if kind == "artifact":
        if data.artifact_id is None or data.artifact_run_id is None or data.verdict is not None:
            raise ValueError("artifact result requires the exact saved artifact and run")
        saved = _artifact(tools, scope, data.artifact_run_id, data.artifact_id, own=True)
        body = json.dumps({"summary": body, "artifact": saved}, sort_keys=True)
        artifacts = [{key: value for key, value in saved.items() if key != "content"}]
    elif kind == "qa_review":
        if scope["profile_kind"] != "builtin" or scope["profile_name"] != "QA" or data.verdict is None:
            raise Conflict("QA review requires the assigned QA agent and an explicit verdict")
        with tools.gateway.engine.connect() as connection:
            inputs = (
                connection.execute(
                    text(
                        "SELECT previous.acceptance_kind,previous.status,m.body FROM "
                        "task_session_assignments qa "
                        "JOIN task_session_dependencies d ON d.project_id=qa.project_id AND "
                        "d.assignment_id=qa.id "
                        "JOIN task_session_assignments previous ON previous.project_id=d.project_id "
                        "AND previous.id=d.depends_on_id LEFT JOIN task_session_messages m "
                        "ON m.project_id=previous.project_id AND m.id=previous.result_message_id "
                        "WHERE qa.project_id=:p AND qa.child_session_id=:s"
                    ),
                    {"p": tools.project_id, "s": tools.session_id},
                )
                .mappings()
                .all()
            )
        if not inputs or any(item["status"] != "satisfied" for item in inputs):
            raise Conflict("QA inputs are not confirmed")
        for item in inputs:
            if item["acceptance_kind"] == "artifact":
                reference = json.loads(item["body"])["artifact"]
                verified = _artifact(
                    tools, scope, UUID(reference["run_id"]), UUID(reference["artifact_id"]), own=False
                )
                if verified["sha256"] != reference["sha256"]:
                    raise Conflict("QA artifact identity changed")
                if (
                    verified["kind"] == "video"
                    and data.verdict == "passed"
                    and not verified.get("playback_verified")
                ):
                    raise Conflict(
                        "the operator must verify playback of the exact video before QA can pass it"
                    )
                artifacts.append({key: value for key, value in verified.items() if key != "content"})
        body = json.dumps(
            {"verdict": data.verdict, "reviewed_artifacts": artifacts, "feedback": body}, sort_keys=True
        )
        if data.verdict == "needs_rework":
            message = tools._publish(scope, action_id, body)
            with tools.gateway.engine.begin() as connection:
                lock_project(connection, tools.project_id)
                connection.execute(
                    text(
                        "UPDATE task_session_assignments SET status='blocked' WHERE project_id=:p "
                        "AND child_session_id=:s AND status='active'"
                    ),
                    {"p": tools.project_id, "s": tools.session_id},
                )
                # Saved result identities remain satisfied: QA rejection is a quality
                # decision about those bytes, not a deletion of the reporting receipt.
                connection.execute(
                    text(
                        "UPDATE work_orders SET status='failed' WHERE project_id=:p AND id IN "
                        "(SELECT s.work_order_id FROM task_sessions s JOIN task_session_assignments a "
                        "ON a.project_id=s.project_id AND a.child_session_id=s.id WHERE a.project_id=:p "
                        "AND (a.child_session_id=:session OR (a.acceptance_kind='artifact' AND a.id IN "
                        "(SELECT d.depends_on_id FROM task_session_dependencies d "
                        "JOIN task_session_assignments qa ON qa.project_id=d.project_id "
                        "AND qa.id=d.assignment_id WHERE qa.project_id=:p "
                        "AND qa.child_session_id=:session)))) "
                        "AND status IN ('ready','running','needs_review')"
                    ),
                    {"p": tools.project_id, "session": tools.session_id},
                )
                chief = TaskSessionDecisions._main(connection, tools.project_id, scope["root_work_order_id"])
                sequence = tools.sessions._sequence(connection, tools.project_id, chief["id"]) + 1
                from uuid import NAMESPACE_URL, uuid5

                connection.execute(
                    text(
                        "INSERT INTO task_session_messages "
                        "(id,project_id,actor_id,root_work_order_id,session_id,"
                        "source_session_id,sequence,kind,body) VALUES "
                        "(:id,:p,:actor,:root,:chief,:source,:seq,'agent',:body)"
                    ),
                    {
                        "id": uuid5(NAMESPACE_URL, f"qa-feedback:{action_id}"),
                        "p": tools.project_id,
                        "actor": f"profile:{scope['profile_id']}",
                        "root": scope["root_work_order_id"],
                        "chief": chief["id"],
                        "source": tools.session_id,
                        "seq": sequence,
                        "body": body,
                    },
                )
                TaskSessionDecisions(tools.gateway).record_blocker(
                    connection, tools.project_id, tools.session_id, tools.attempt_id, "qa_needs_rework"
                )
                tools.gateway._event(
                    connection,
                    tools.project_id,
                    scope["work_order_id"],
                    tools.attempt_id,
                    "task_session_qa_review",
                    {
                        "tool_call_id": str(action_id),
                        "verdict": data.verdict,
                        "message_id": message["message_id"],
                        "reviewed_artifacts": artifacts,
                    },
                )
            return {**message, "status": "needs_rework", "reviewed_artifacts": artifacts}
    elif kind != "text_report" or any(
        value is not None for value in (data.artifact_id, data.artifact_run_id, data.verdict)
    ):
        raise Conflict("this result requires its declared evidence owner")
    saved_result = TaskSessionAssignments(tools.gateway).report_text_result(
        tools.project_id, tools.session_id, tools.attempt_id, action_id, body, evidence_kind=kind
    )
    return {
        "assignment_id": str(saved_result["assignment_id"]),
        "result_message_id": str(saved_result["result_message_id"]),
        "status": "saved",
        "reviewed_artifacts": artifacts if kind == "qa_review" else [],
    }


def submit_completion(
    tools: TaskSessionTools, scope: Mapping[str, Any], action_id: UUID, data: CompletionArgs
) -> dict[str, Any]:
    """Use the explicitly saved completion policy after verifying its exact evidence."""
    body = local_text(data.body)
    if contains_sensitive_text(body):
        raise ValueError("sensitive result")
    artifact = None
    if data.artifact_id is not None and data.artifact_run_id is not None:
        artifact = _artifact(tools, scope, data.artifact_run_id, data.artifact_id, own=False)
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        from onefoundry.self_extension_resume import require_resumed

        require_resumed(connection, tools.project_id, scope["root_work_order_id"])
        order = (
            connection.execute(
                text(
                    "SELECT deliverable_kind,status FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"
                ),
                {"p": tools.project_id, "o": scope["root_work_order_id"]},
            )
            .mappings()
            .one()
        )
        if order["status"] != "running":
            raise Conflict("work order is not awaiting team completion")
        incomplete = connection.scalar(
            text(
                "SELECT 1 FROM task_session_assignments WHERE project_id=:p AND root_work_order_id=:o "
                "AND status NOT IN ('satisfied','canceled') LIMIT 1"
            ),
            {"p": tools.project_id, "o": scope["root_work_order_id"]},
        )
        if incomplete is not None:
            raise Conflict("team results are still missing or blocked")
        if unresolved(connection, tools.project_id, root_id=scope["root_work_order_id"]):
            raise Conflict("operator acknowledgement of unknown outcomes is still missing")
        pending = connection.scalar(
            text(
                "SELECT 1 FROM approval_requests a JOIN work_orders w ON w.project_id=a.project_id "
                "AND w.id=a.work_order_id WHERE a.project_id=:p AND a.status='pending' "
                "AND (w.id=:o OR w.parent_work_order_id=:o) LIMIT 1"
            ),
            {"p": tools.project_id, "o": scope["root_work_order_id"]},
        )
        if pending is not None:
            raise Conflict("operator approval is still pending")
        if order["deliverable_kind"] in ("code_change", "external_action"):
            raise Conflict("completion belongs to the separate execution or outbound owner")
        chief_verified = (
            chief_verified_completion.policy(connection, tools.project_id, scope["root_work_order_id"])
            and order["deliverable_kind"] != "operator_review"
        )
        if chief_verified:
            chief_verified_completion.verify(connection, tools, scope, action_id)
        if order["deliverable_kind"] in ("artifact", "document") or (chief_verified and artifact is not None):
            if artifact is None:
                raise Conflict("a saved artifact is required; a text reply is insufficient")
            if artifact["kind"] == "video" and not artifact.get("playback_verified"):
                raise Conflict("the exact recorded video still needs operator playback verification")
            reviewed = connection.scalar(
                text(
                    "SELECT 1 FROM task_session_tool_calls c JOIN task_session_assignments qa "
                    "ON qa.project_id=c.project_id AND qa.child_session_id=c.session_id "
                    "WHERE c.project_id=:p AND c.root_work_order_id=:o AND "
                    "c.tool_name='onefoundry.report_result' "
                    "AND c.status='done' AND qa.acceptance_kind='qa_review' AND qa.status='satisfied' "
                    "AND EXISTS(SELECT 1 FROM jsonb_array_elements(c.result->'reviewed_artifacts') x "
                    "WHERE x->>'artifact_id'=:id AND x->>'sha256'=:digest) LIMIT 1"
                ),
                {
                    "p": tools.project_id,
                    "o": scope["root_work_order_id"],
                    "id": artifact["artifact_id"],
                    "digest": artifact["sha256"],
                },
            )
            if reviewed is None:
                raise Conflict("the exact final artifact still needs a passed QA review")
        final_status = "done" if chief_verified else "needs_review"
        published = tools._publish_in_transaction(connection, scope, action_id, body)
        connection.execute(
            text("UPDATE work_orders SET status=:status WHERE project_id=:p AND id=:o"),
            {"p": tools.project_id, "o": scope["root_work_order_id"], "status": final_status},
        )
        if chief_verified:
            chief_verified_completion.close_local_children(
                tools.gateway,
                connection,
                tools.project_id,
                scope["root_work_order_id"],
                "Chief verified accepted local work and independent QA",
            )

        tools.gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "task_session_completion_submitted",
            {
                "tool_call_id": str(action_id),
                "summary": body,
                "artifact": (
                    {key: value for key, value in artifact.items() if key != "content"} if artifact else None
                ),
                "status": final_status,
                "message_id": published["message_id"],
                "acceptance_owner": "chief_verified" if chief_verified else "operator_review",
            },
        )
    return {
        "status": final_status,
        "next_step": "complete" if chief_verified else "operator_acceptance",
        **published,
    }
