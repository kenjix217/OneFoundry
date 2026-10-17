"""Chief proposes exact maintenance setup in the existing approval inbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import RepositoryCodeTarget, digest
from onefoundry.repository_code_gateway import frozen_target

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


def propose(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, target: RepositoryCodeTarget
) -> dict[str, Any]:
    if target.task_scope_id != scope["root_work_order_id"]:
        raise Conflict("maintenance setup must belong to this original task")
    target = frozen_target(tools.gateway, tools.project_id, target)
    content = target.model_dump_json()
    label = "Approve one bounded maintenance coding attempt"
    proposal_digest = digest({"action_type": "other", "target": label, "content": content})
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='self_extension_target_proposed' "
                "AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            if prior["proposal_digest"] != proposal_digest:
                raise Conflict("maintenance proposal identity changed")
            return dict(prior)
        approval_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,run_id,"
                "action_type,target,content,proposal_digest,status) "
                "VALUES (:id,:p,:actor,:order,:run,'other',:target,:content,:digest,'pending')"
            ),
            {
                "id": approval_id,
                "p": tools.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "order": scope["work_order_id"],
                "run": tools.attempt_id,
                "target": label,
                "content": content,
                "digest": proposal_digest,
            },
        )
        result = {
            "tool_call_id": str(action_id),
            "approval_id": str(approval_id),
            "proposal_digest": proposal_digest,
            "status": "waiting_for_operator",
        }
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "self_extension_target_proposed",
            result,
        )
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "approval_requested",
            {
                "approval_id": str(approval_id),
                "action_type": "other",
                "proposal_digest": proposal_digest,
                "status": "pending",
            },
        )
        return result
