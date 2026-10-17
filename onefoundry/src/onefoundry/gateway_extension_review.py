"""Exact inbox binding for package activation and revocation."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from onefoundry import run_audit
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.work_order_readiness import mark_ready
from onefoundry.workflow_schemas import WorkOrderCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _review_context(
    gateway: ToolGateway, connection: Connection, project_id: UUID, title: str
) -> tuple[UUID, UUID]:
    profile_id = connection.scalar(
        text("SELECT id FROM profiles WHERE project_id=:p AND kind='builtin' AND name='Chief of Staff'"),
        {"p": project_id},
    )
    if profile_id is None:
        raise Conflict("Chief of Staff profile is unavailable for local review")
    order = gateway.create_work_order_in_transaction(
        connection,
        project_id,
        WorkOrderCreate(
            title=title,
            instructions=(
                "Local operator review of an exact extension package change. No model turn is queued."
            ),
            profile_id=profile_id,
            deliverable_kind="operator_review",
            acceptance_checks=["Decide the exact package hash and current revision in Approvals."],
        ),
    )
    order_id: UUID = order["id"]
    mark_ready(gateway, connection, project_id, order_id)
    run_id = uuid4()
    run_audit.insert_run(
        connection, project_id, run_id, order_id, profile_id, gateway.actor_id, "manual_planning"
    )
    connection.execute(
        text("UPDATE work_orders SET status='running' WHERE project_id=:p AND id=:id"),
        {"p": project_id, "id": order_id},
    )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "run_started",
        {"status": "running", "profile_id": str(profile_id), "mode": "manual_planning"},
    )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "extension_review_context_created",
        {"reason": "operator_package_review", "model_turn": False},
    )
    return order_id, run_id


def request_review(
    gateway: ToolGateway,
    connection: Connection,
    project_id: UUID,
    package: dict[str, Any],
    action: str,
    source_run_id: UUID | None,
) -> dict[str, Any]:
    existing = (
        connection.execute(
            text(
                "SELECT b.approval_id,a.status,a.work_order_id FROM extension_review_bindings b "
                "JOIN approval_requests a ON a.project_id=b.project_id AND a.id=b.approval_id "
                "WHERE b.project_id=:p AND b.package_id=:id AND b.action=:action "
                "AND b.expected_revision=:revision"
            ),
            {"p": project_id, "id": package["id"], "action": action, "revision": package["revision"]},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        return {
            "approval_id": existing["approval_id"],
            "status": existing["status"],
            "review_work_order_id": existing["work_order_id"],
        }
    title = f"Review {action} for {package['package_key']} {package['package_version']}"
    if source_run_id is None:
        order_id, run_id = _review_context(gateway, connection, project_id, title)
    else:
        source = (
            connection.execute(
                text(
                    "SELECT r.work_order_id,r.status,w.status AS order_status "
                    "FROM runs r JOIN work_orders w ON w.project_id=r.project_id AND w.id=r.work_order_id "
                    "WHERE r.project_id=:p AND r.id=:id FOR UPDATE OF r,w"
                ),
                {"p": project_id, "id": source_run_id},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise NotFound("source run not found")
        if source["status"] != "running" or source["order_status"] != "running":
            raise Conflict("source run is not active")
        order_id, run_id = source["work_order_id"], source_run_id
    content = json.dumps(
        {
            "kind": "extension_package_review_v1",
            "action": action,
            "package": package["package_key"],
            "version": package["package_version"],
            "sha256": package["sha256"],
            "revision": package["revision"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal = {"action_type": "other", "target": title, "content": content}
    digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    approval_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO approval_requests "
            "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,proposal_digest,status) "
            "VALUES (:id,:p,:actor,:order,:run,'other',:target,:content,:digest,'pending')"
        ),
        {
            "id": approval_id,
            "p": project_id,
            "actor": gateway.actor_id,
            "order": order_id,
            "run": run_id,
            "target": title,
            "content": content,
            "digest": digest,
        },
    )
    connection.execute(
        text(
            "INSERT INTO extension_review_bindings "
            "(project_id,approval_id,actor_id,package_id,action,expected_sha256,expected_revision) "
            "VALUES (:p,:approval,:actor,:package,:action,:sha,:revision)"
        ),
        {
            "p": project_id,
            "approval": approval_id,
            "actor": gateway.actor_id,
            "package": package["id"],
            "action": action,
            "sha": package["sha256"],
            "revision": package["revision"],
        },
    )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "approval_requested",
        {
            "approval_id": str(approval_id),
            "action_type": "other",
            "proposal_digest": digest,
            "status": "pending",
        },
    )
    gateway._event(
        connection,
        project_id,
        order_id,
        run_id,
        "extension_package_review_requested",
        {
            "approval_id": str(approval_id),
            "package_id": str(package["id"]),
            "action": action,
            "sha256": package["sha256"],
        },
    )
    return {"approval_id": approval_id, "status": "pending", "review_work_order_id": order_id}


def lock_review_project(
    gateway: ToolGateway, connection: Connection, project_id: UUID, approval_id: UUID
) -> None:
    """Take the package-review project lock before approval/work-order locks."""
    bound = connection.scalar(
        text("SELECT 1 FROM extension_review_bindings WHERE project_id=:p AND approval_id=:approval"),
        {"p": project_id, "approval": approval_id},
    )
    if bound:
        from onefoundry.gateway_extension_store import _lock_project

        _lock_project(gateway, connection, project_id)


def apply_decision(
    gateway: ToolGateway, connection: Connection, project_id: UUID, approval: dict[str, Any], decision: str
) -> None:
    bound = (
        connection.execute(
            text(
                "SELECT package_id,action,expected_sha256,expected_revision "
                "FROM extension_review_bindings WHERE project_id=:p AND approval_id=:approval"
            ),
            {"p": project_id, "approval": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if bound is None:
        return
    from onefoundry.gateway_extension_store import _lock_project

    _lock_project(gateway, connection, project_id)
    package = (
        connection.execute(
            text(
                "SELECT id,package_key,package_version,sha256,state,revision,manifest "
                "FROM extension_packages WHERE project_id=:p AND id=:id FOR UPDATE"
            ),
            {"p": project_id, "id": bound["package_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if package is None:
        raise Conflict("reviewed extension package is unavailable")
    if package["sha256"] != bound["expected_sha256"] or package["revision"] != bound["expected_revision"]:
        raise Conflict("reviewed extension package changed")
    if decision == "rejected":
        gateway._event(
            connection,
            project_id,
            approval["work_order_id"],
            approval["run_id"],
            "extension_package_review_rejected",
            {"approval_id": str(approval["id"]), "package_id": str(package["id"])},
        )
        _close_local_context(gateway, connection, project_id, approval)
        return
    from onefoundry.gateway_extension_identity import canonical_tool_id
    from onefoundry.role_tool_admission import registered_tool_ids

    if bound["action"] == "activate":
        if package["state"] not in {"draft", "revoked"}:
            raise Conflict("package is already active")
        active_version = connection.scalar(
            text(
                "SELECT 1 FROM extension_packages WHERE project_id=:p AND package_key=:key "
                "AND state='active' AND id<>:id"
            ),
            {"p": project_id, "key": package["package_key"], "id": package["id"]},
        )
        if active_version:
            raise Conflict("another version of this extension is active")
        declared_ids = [
            canonical_tool_id(package["package_key"], tool["id"])
            for tool in package["manifest"].get("tools", [])
        ]
        declared = set(declared_ids)
        if len(declared) != len(declared_ids):
            raise Conflict("extension tools have duplicate installed identities")
        declared_names = {
            identity: tool["id"]
            for identity, tool in zip(declared_ids, package["manifest"].get("tools", []), strict=True)
        }
        if declared & registered_tool_ids():
            raise Conflict("extension tool conflicts with a built-in tool")
        others = connection.execute(
            text(
                "SELECT package_key,manifest FROM extension_packages "
                "WHERE project_id=:p AND state IN ('active','revoked') AND id<>:id"
            ),
            {"p": project_id, "id": package["id"]},
        ).mappings()
        for other in others:
            for tool in other["manifest"].get("tools", []):
                identity = canonical_tool_id(other["package_key"], tool["id"])
                if identity in declared_names and (
                    other["package_key"] != package["package_key"] or tool["id"] != declared_names[identity]
                ):
                    raise Conflict("extension tool conflicts with another reviewed package")
        next_state = "active"
    else:
        if package["state"] != "active":
            raise Conflict("package is not active")
        next_state = "revoked"
    connection.execute(
        text("UPDATE extension_packages SET state=:state,revision=revision+1 WHERE project_id=:p AND id=:id"),
        {"state": next_state, "p": project_id, "id": package["id"]},
    )
    if next_state == "active":
        from onefoundry.gateway_extension_skills import accept_package_skills

        accept_package_skills(gateway, connection, project_id, dict(package))
    gateway._event(
        connection,
        project_id,
        approval["work_order_id"],
        approval["run_id"],
        "extension_package_state_changed",
        {
            "approval_id": str(approval["id"]),
            "package_id": str(package["id"]),
            "state": next_state,
            "sha256": package["sha256"],
            "revision": package["revision"] + 1,
        },
    )
    _close_local_context(gateway, connection, project_id, approval)


def _close_local_context(
    gateway: ToolGateway, connection: Connection, project_id: UUID, approval: dict[str, Any]
) -> None:
    local = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:p AND run_id=:run "
            "AND event_type='extension_review_context_created' LIMIT 1"
        ),
        {"p": project_id, "run": approval["run_id"]},
    )
    if not local:
        return
    connection.execute(
        text(
            "UPDATE runs SET status='done',outcome_reason='extension_review_decided',"
            "finished_at=clock_timestamp() WHERE project_id=:p AND id=:run AND status='running'"
        ),
        {"p": project_id, "run": approval["run_id"]},
    )
    connection.execute(
        text("UPDATE work_orders SET status='done' WHERE project_id=:p AND id=:order AND status='running'"),
        {"p": project_id, "order": approval["work_order_id"]},
    )
    gateway._event(
        connection,
        project_id,
        approval["work_order_id"],
        approval["run_id"],
        "extension_review_context_closed",
        {"approval_id": str(approval["id"]), "status": "done"},
    )
    gateway._event(
        connection,
        project_id,
        approval["work_order_id"],
        approval["run_id"],
        "run_finished",
        {"outcome": "done", "reason": "extension_review_decided"},
    )
