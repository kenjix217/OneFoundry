"""Execution Gateway owner for approved local maintenance targets and dispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text

from onefoundry import run_audit
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import (
    TARGET,
    RepositoryCodeLaunch,
    RepositoryCodeManifest,
    RepositoryCodeTarget,
    RepositoryRunnerCapability,
    digest,
)
from onefoundry.runner_spool import SpoolInvalid
from onefoundry.source_store import verify_local

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from onefoundry.execution_gateway import ExecutionGateway
    from onefoundry.tool_gateway import ToolGateway


class RepositoryManifestTooLarge(Conflict):
    """Known input rejection before a run, job or provider launch is created."""


def current_source(connection: Connection, project_id: UUID, target: RepositoryCodeTarget) -> dict[str, str]:
    row = (
        connection.execute(
            text(
                "SELECT kind,root_id,relative_path,branch,revision FROM project_sources "
                "WHERE project_id=:p AND id=:id FOR KEY SHARE"
            ),
            {"p": project_id, "id": target.source_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("maintenance source not found in this project")
    if row["kind"] != "local" or row["root_id"] != "workspace" or row["revision"] != target.revision:
        raise Conflict("maintenance source must be the selected exact local revision")
    return {
        "source_id": str(target.source_id),
        "root_id": row["root_id"],
        "relative_path": row["relative_path"],
        "branch": row["branch"],
        "revision": row["revision"],
    }


def frozen_target(
    gateway: ToolGateway, project_id: UUID, target: RepositoryCodeTarget
) -> RepositoryCodeTarget:
    with gateway.engine.connect() as connection:
        source = current_source(connection, project_id, target)
        if (
            connection.scalar(
                text("SELECT 1 FROM work_orders WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": target.task_scope_id},
            )
            is None
        ):
            raise NotFound("maintenance task scope not found in this project")
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("maintenance source configuration is unavailable")
    if verify_local(settings, source["relative_path"]) != (source["branch"], source["revision"]):
        raise Conflict("maintenance source changed; select the repository again")
    with gateway.engine.connect() as connection:
        profile = (
            connection.execute(
                text(
                    "SELECT model,reasoning_effort FROM profiles "
                    "WHERE project_id=:p AND kind='builtin' AND name='Engineer'"
                ),
                {"p": project_id},
            )
            .mappings()
            .one_or_none()
        )
    if profile is None or not profile["model"]:
        raise Conflict("configure the built-in Engineer model before reviewing a coding pilot")
    target = RepositoryCodeTarget.model_validate(
        {
            **target.model_dump(mode="json"),
            "model_id": profile["model"],
            "reasoning_effort": profile["reasoning_effort"],
            "execution_policy": settings.repository_code_policy.model_dump(mode="json"),
            "output_limits": {
                key: settings.runner_limits[key] for key in ("max_log_bytes", "max_diff_bytes")
            },
        }
    )
    return target


def propose(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, target: RepositoryCodeTarget
) -> dict[str, Any]:
    """Operator-visible scope proposal. This grants no permission until approved."""
    from onefoundry.workflow_schemas import ApprovalCreate

    target = frozen_target(gateway, project_id, target)
    return gateway.request_approval(
        project_id,
        run_id,
        ApprovalCreate(
            action_type="other",
            target=f"One maintenance coding attempt for {target.source_id}@{target.revision}",
            content=target.model_dump_json(),
        ),
    )


def validate_pending(gateway: ToolGateway, connection: Any, project_id: UUID, approval: Any) -> None:
    try:
        value = json.loads(approval["content"])
    except ValueError:
        return
    if not isinstance(value, dict) or value.get("kind") != "repository_code_target_v1":
        return
    try:
        target = RepositoryCodeTarget.model_validate(value)
    except ValidationError as exc:
        raise Conflict("maintenance approval target is invalid") from exc
    current_source(connection, project_id, target)
    if frozen_target(gateway, project_id, target) != target:
        raise Conflict("maintenance source, model or limits changed before approval")


def approved_target(connection: Connection, project_id: UUID, approval_id: UUID) -> RepositoryCodeTarget:
    row = (
        connection.execute(
            text(
                "SELECT status,action_type,content FROM approval_requests WHERE project_id=:p"
                " AND id=:id FOR SHARE"
            ),
            {"p": project_id, "id": approval_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("maintenance approval not found")
    if row["status"] != "approved" or row["action_type"] != "other":
        raise Conflict("exact maintenance target requires operator approval")
    try:
        return RepositoryCodeTarget.model_validate_json(row["content"])
    except ValidationError as exc:
        raise Conflict("approval does not describe a maintenance target") from exc


def capability(execution: ExecutionGateway, adapter_id: str) -> RepositoryRunnerCapability:
    try:
        raw = execution.spool._read_regular(execution.spool.root / "repository_capabilities.json", 4096)
        report = RepositoryRunnerCapability.model_validate_json(raw)
        age = (datetime.now(UTC) - report.observed_at).total_seconds()
        if report.adapter_id != adapter_id or not 0 <= age <= execution.capability_max_age_seconds:
            raise ValueError("stale or different adapter")
        return report
    except (OSError, ValueError, TypeError, SpoolInvalid) as exc:
        raise Unavailable("isolated maintenance runner has no fresh capability proof") from exc


def launch(
    execution: ExecutionGateway, project_id: UUID, order_id: UUID, data: RepositoryCodeLaunch
) -> dict[str, Any]:
    gateway = execution.gateway
    settings = gateway.live_text_settings
    if settings is None or not execution.enabled:
        raise Unavailable("external coding configuration is unavailable or disabled")
    intent = digest({"order_id": str(order_id), **data.model_dump(mode="json", exclude={"idempotency_key"})})
    run_id, attempt_id = uuid4(), uuid4()
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        prior = (
            connection.execute(
                text(
                    "SELECT run_id,intent_sha256,fixture_id FROM external_code_attempts "
                    "WHERE project_id=:p AND idempotency_key=:key"
                ),
                {"p": project_id, "key": data.idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["intent_sha256"] != intent or prior["fixture_id"] != TARGET:
                raise Conflict("maintenance action identity was reused for different work")
            return execution.detail(project_id, prior["run_id"])
        rejected = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_dispatch_rejected' "
                "AND payload->>'tool_call_id'=:key LIMIT 1"
            ),
            {"p": project_id, "key": str(data.idempotency_key)},
        )
        if rejected is not None:
            raise Conflict("maintenance launch intent was rejected; plan a replacement")
        target = approved_target(connection, project_id, data.target_approval_id)
        if data.adapter_id != target.adapter_id:
            raise Conflict("coding adapter differs from the approved maintenance pilot")
        used = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='repository_code_queued' "
                "AND payload->'manifest'->>'target_approval_id'=:approval LIMIT 1"
            ),
            {"p": project_id, "approval": str(data.target_approval_id)},
        )
        if used is not None:
            raise Conflict("this one-attempt maintenance approval has been used; review another exact pilot")
        source = current_source(connection, project_id, target)
        order = (
            connection.execute(
                text(
                    "SELECT w.*,p.name AS profile_name,p.kind AS profile_kind,p.model AS coding_model, "
                    "p.reasoning_effort AS coding_reasoning_effort "
                    "FROM work_orders w "
                    "JOIN profiles p "
                    "ON p.project_id=w.project_id AND p.id=w.profile_id WHERE w.project_id=:p AND"
                    " w.id=:id FOR UPDATE OF w"
                ),
                {"p": project_id, "id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("Engineer work order not found")
        root_id = connection.scalar(
            text(
                "SELECT root_work_order_id FROM task_sessions WHERE project_id=:p "
                "AND work_order_id=:id LIMIT 1"
            ),
            {"p": project_id, "id": order_id},
        )
        if target.task_scope_id != (root_id or order_id):
            raise Conflict("maintenance target is outside the approved original task")
        if order["status"] not in ("ready", "running") or (order["profile_kind"], order["profile_name"]) != (
            "builtin",
            "Engineer",
        ):
            raise Conflict("maintenance coding requires an active built-in Engineer work order")
        uncertain = connection.scalar(
            text(
                "SELECT 1 FROM external_code_attempts a JOIN runs r ON r.project_id=a.project"
                "_id AND r.id=a.run_id "
                "WHERE a.project_id=:p AND r.work_order_id=:id AND a.status IN ('queued','unk"
                "nown','done') LIMIT 1"
            ),
            {"p": project_id, "id": order_id},
        )
        if uncertain is not None:
            raise Conflict("reconcile the existing coding attempt; automatic relaunch is forbidden")
        if (
            connection.scalar(
                text(
                    "SELECT 1 FROM runs WHERE project_id=:p AND work_order_id=:id "
                    "AND status='running' LIMIT 1"
                ),
                {"p": project_id, "id": order_id},
            )
            is not None
        ):
            raise Conflict("finish the active planning/model run before dispatching external coding")
        report = capability(execution, data.adapter_id)
        if (
            report.model_id != target.model_id
            or report.provider_url != target.provider_url
            or order["coding_model"] != target.model_id
            or order["coding_reasoning_effort"] != target.reasoning_effort
            or target.execution_policy != settings.repository_code_policy
            or target.output_limits
            != {key: execution.limits[key] for key in ("max_log_bytes", "max_diff_bytes")}
        ):
            raise Conflict("runner, model or execution bounds changed; review a new exact coding pilot")
        if verify_local(settings, source["relative_path"]) != (source["branch"], source["revision"]):
            raise Conflict("maintenance source changed; select the repository again")
        unsigned = {
            "schema_version": 4,
            "target_id": TARGET,
            "job_id": str(attempt_id),
            "project_id": str(project_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "work_order_id": str(order_id),
            "actor_id": gateway.actor_id,
            "adapter_id": data.adapter_id,
            "model_id": order["coding_model"],
            "task_title": order["title"],
            "task_instructions": order["instructions"],
            "target_approval_id": str(data.target_approval_id),
            "target": target.model_dump(mode="json"),
            "source": source,
            "idempotency_key": str(data.idempotency_key),
            "limits": {**execution.limits, "wall_seconds": settings.repository_code_policy.wall_seconds},
            "stage_limits": settings.repository_code_policy.model_dump(mode="json"),
            "intent_sha256": intent,
        }
        manifest = RepositoryCodeManifest.model_validate({**unsigned, "request_sha256": digest(unsigned)})
        payload = manifest.model_dump(mode="json")
        if len(json.dumps(payload).encode()) > 8192:
            raise RepositoryManifestTooLarge("maintenance manifest exceeds the runner bound")
        run_audit.insert_run(
            connection, project_id, run_id, order_id, order["profile_id"], gateway.actor_id, "external_code"
        )
        connection.execute(
            text("UPDATE work_orders SET status='running' WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO external_code_attempts (id,project_id,run_id,actor_id,adapter_id,fixture_id,"
                "request_sha256,status,source_id,source_root_id,source_relative_path,source_b"
                "ranch,source_revision,"
                "idempotency_key,intent_sha256) VALUES (:id,:p,:run,:actor,:adapter,:target,:"
                "sha,'queued',:source_id,"
                ":root,:path,:branch,:revision,:key,:intent)"
            ),
            {
                "id": attempt_id,
                "p": project_id,
                "run": run_id,
                "actor": gateway.actor_id,
                "adapter": data.adapter_id,
                "target": TARGET,
                "sha": manifest.request_sha256,
                "source_id": target.source_id,
                "root": source["root_id"],
                "path": source["relative_path"],
                "branch": source["branch"],
                "revision": target.revision,
                "key": data.idempotency_key,
                "intent": intent,
            },
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            run_id,
            "repository_code_queued",
            {"attempt_id": str(attempt_id), "intent_sha256": intent, "manifest": payload},
        )
    try:
        from runner.repository_transport import dispatch_job

        dispatch_job(execution.spool, attempt_id, payload)
    except (OSError, SpoolInvalid):
        execution._mark_unknown(project_id, run_id, attempt_id, "manifest_dispatch_uncertain")
    return execution.detail(project_id, run_id)
