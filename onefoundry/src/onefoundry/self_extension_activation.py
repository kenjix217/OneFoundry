"""Exact independent QA and release proposals in the existing approval inbox."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.extension_activation_contract import (
    ExtensionActivationJob,
    ExtensionRelease,
    ExtensionReleaseRecord,
)
from onefoundry.extension_qa_contract import ExtensionQaReceipt
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest
from onefoundry.repository_code_gateway import approved_target, current_source
from onefoundry.repository_code_observation import saved_manifest
from onefoundry.self_extension_results import _coding

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools
    from onefoundry.tool_gateway import ToolGateway


def validate_pending(gateway: ToolGateway, connection: Any, project_id: UUID, approval: Any) -> None:
    try:
        value = json.loads(approval["content"])
    except ValueError:
        return
    if not isinstance(value, dict) or value.get("kind") != "extension_activation_proposal_v1":
        return
    if approval["action_type"] != "deploy":
        raise Conflict("activation approval action changed")
    if (
        digest({"action_type": "deploy", "target": approval["target"], "content": approval["content"]})
        != approval["proposal_digest"]
    ):
        raise Conflict("activation proposal identity changed")
    manifest = saved_manifest(
        gateway._execution_gateway(), project_id, UUID(value["release"]["coding_run_id"])
    )
    current_source(
        connection, project_id, approved_target(connection, project_id, manifest.target_approval_id)
    )
    for grant in value.get("tool_grants", []):
        current = connection.scalar(
            text("SELECT allowed_tools FROM profiles WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": UUID(grant["profile_id"])},
        )
        if current != grant["tools_before"]:
            raise Conflict("original profile permissions changed before activation approval")


def propose(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, data: ExtensionRelease
) -> dict[str, Any]:
    detail, _ = _coding(tools, scope, data.coding_run_id)
    candidate = detail["attempts"][-1]
    gateway = tools.gateway
    execution = gateway._execution_gateway()
    try:
        raw = execution.spool._read_regular(
            execution.spool.root / "artifacts" / str(candidate["id"]) / "release.json", 8192
        )
        release_record = ExtensionReleaseRecord.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise Conflict("the exact prebuilt release has no separate supervisor verification") from exc
    if (
        release_record.project_id != tools.project_id
        or release_record.candidate_commit != candidate["commit"]
        or release_record.diff_sha256 != candidate["diff_sha256"]
        or release_record.release != data
    ):
        raise Conflict("verified release differs from this exact candidate or release proposal")
    manifest = saved_manifest(gateway._execution_gateway(), tools.project_id, data.coding_run_id)
    with gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        current_source(
            connection,
            tools.project_id,
            approved_target(connection, tools.project_id, manifest.target_approval_id),
        )
        verified = connection.execute(
            text(
                "SELECT e.payload FROM events e JOIN task_session_assignments a ON a.project_id=e.project_id "
                "AND a.child_session_id::text=e.payload->>'qa_session_id' JOIN profiles"
                " p ON p.project_id=e.project_id "
                "AND p.id::text=e.payload->>'qa_profile_id' WHERE e.project_id=:p AND e"
                ".event_type='self_extension_qa_verified' "
                "AND e.payload->>'qa_job_id'=:job AND a.status='satisfied' "
                "AND p.kind='builtin' AND p.name='QA' "
                "AND a.root_work_order_id=:root ORDER BY e.created_at DESC LIMIT 1"
            ),
            {"p": tools.project_id, "job": str(data.qa_job_id), "root": scope["root_work_order_id"]},
        ).scalar()
        if verified is None:
            raise Conflict("activation needs confirmed independent QA for this task")
        qa = ExtensionQaReceipt.model_validate(verified["receipt"])
        if release_record.qa_receipt_sha256 != verified["receipt_sha256"]:
            raise Conflict("release verification used a different independent QA receipt")
        if (
            qa.status != "passed"
            or qa.candidate_commit != candidate["commit"]
            or qa.diff_sha256 != candidate["diff_sha256"]
        ):
            raise Conflict("QA does not accept this exact candidate")
        request = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='self_ex"
                "tension_requested' "
                "AND payload->>'original_work_order_id'=:root AND payload->>'target_app"
                "roval_id'=:target LIMIT 1"
            ),
            {
                "p": tools.project_id,
                "root": str(scope["root_work_order_id"]),
                "target": str(manifest.target_approval_id),
            },
        )
        if request is None:
            raise Conflict("original extension checkpoint is unavailable")
        content_value = {
            "kind": "extension_activation_proposal_v1",
            "candidate_commit": candidate["commit"],
            "diff_sha256": candidate["diff_sha256"],
            "qa_receipt_sha256": verified["receipt_sha256"],
            "release": data.model_dump(mode="json"),
            "original_work_order_id": request["original_work_order_id"],
            "operation": request["operation"],
            "gap_source_id": request["gap_source_id"],
            "target_approval_id": str(manifest.target_approval_id),
        }
        content_value["tool_grants"] = []
        if request["operation"].startswith("tool:"):
            profile = (
                connection.execute(
                    text(
                        "SELECT p.id,p.allowed_tools FROM task_sessions s JOIN profiles p "
                        "ON p.project_id=s.project_id AND p.id=s.profile_id WHERE s.project_id=:p "
                        "AND s.id=:id"
                    ),
                    {"p": tools.project_id, "id": UUID(request["original_session_id"])},
                )
                .mappings()
                .one()
            )
            content_value["tool_grants"] = [
                {
                    "profile_id": str(profile["id"]),
                    "tool_id": request["operation"][5:],
                    "tools_before": profile["allowed_tools"],
                }
            ]
        content = json.dumps(content_value, sort_keys=True, separators=(",", ":"))
        target = f"Activate OneFoundry {data.version}"
        proposal_digest = digest({"action_type": "deploy", "target": target, "content": content})
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='self_ex"
                "tension_activation_proposed' AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            if prior["proposal_digest"] != proposal_digest:
                raise Conflict("activation action identity changed")
            return dict(prior)
        approval_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,ru"
                "n_id,action_type,target,content,proposal_digest,status) "
                "VALUES (:id,:p,:actor,:order,:run,'deploy',:target,:content,:digest,'pending')"
            ),
            {
                "id": approval_id,
                "p": tools.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "order": scope["work_order_id"],
                "run": tools.attempt_id,
                "target": target,
                "content": content,
                "digest": proposal_digest,
            },
        )
        payload = {
            "tool_call_id": str(action_id),
            "approval_id": str(approval_id),
            "proposal_digest": proposal_digest,
            "candidate_commit": candidate["commit"],
            "status": "waiting_for_operator",
        }
        gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "self_extension_activation_proposed",
            payload,
        )
        gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "approval_requested",
            {
                "approval_id": str(approval_id),
                "action_type": "deploy",
                "proposal_digest": proposal_digest,
                "status": "pending",
            },
        )
        return payload


def dispatch(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    """Only an exact approved inbox decision queues a separate release supervisor."""
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        row = (
            connection.execute(
                text("SELECT * FROM approval_requests WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": project_id, "id": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("activation approval not found")
        if row["status"] != "approved" or row["action_type"] != "deploy":
            raise Conflict("activation requires operator approval of this exact release")
        value = json.loads(row["content"])
        if value.get("kind") != "extension_activation_proposal_v1":
            raise Conflict("approval is not an extension activation")
        manifest = saved_manifest(
            gateway._execution_gateway(), project_id, UUID(value["release"]["coding_run_id"])
        )
        current_source(
            connection, project_id, approved_target(connection, project_id, manifest.target_approval_id)
        )
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='self_ex"
                "tension_activation_queued' AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"p": project_id, "id": str(approval_id)},
        )
        if prior is not None:
            return dict(prior)
        job = ExtensionActivationJob.model_validate(
            {
                "job_id": str(approval_id),
                "project_id": str(project_id),
                "approval_id": str(approval_id),
                "proposal_digest": row["proposal_digest"],
                **{
                    key: value[key]
                    for key in (
                        "candidate_commit",
                        "diff_sha256",
                        "qa_receipt_sha256",
                        "release",
                        "original_work_order_id",
                        "operation",
                        "gap_source_id",
                        "target_approval_id",
                        "tool_grants",
                    )
                },
            }
        )
        payload = {
            "approval_id": str(approval_id),
            "manifest": job.model_dump(mode="json"),
            "status": "queued",
        }
        gateway._event(
            connection,
            project_id,
            row["work_order_id"],
            row["run_id"],
            "self_extension_activation_queued",
            payload,
        )
    from runner.repository_transport import dispatch_job

    dispatch_job(gateway._execution_gateway().spool, approval_id, job.model_dump(mode="json"))
    return payload
