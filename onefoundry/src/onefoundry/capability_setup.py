"""Gateway-owned setup proposals and atomic approval effects, without a second workflow store."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Connection, text

from onefoundry import profile_access
from onefoundry.capability_setup_contract import CapabilitySetup
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools
    from onefoundry.tool_gateway import ToolGateway


def _source(connection: Connection, project_id: UUID, data: CapabilitySetup) -> dict[str, Any] | None:
    repository = any("repository_file_v1" in grant.tool_ids for grant in data.grants)
    if repository and (data.source_id is None or data.revision is None or data.verification_path is None):
        raise Conflict("repository setup needs a selected source, revision and verification file")
    if data.source_id is None:
        if data.revision is not None or data.verification_path is not None:
            raise Conflict("repository verification requires a selected source")
        return None
    row = (
        connection.execute(
            text("SELECT id,kind,revision,display_name FROM project_sources WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": data.source_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("setup source is not in this project")
    if row["revision"] != data.revision:
        raise Conflict("setup source revision changed")
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "revision": row["revision"],
        "display_name": row["display_name"],
    }


def propose(
    tools: TaskSessionTools,
    scope: dict[str, Any],
    action_id: UUID,
    data: CapabilitySetup,
) -> dict[str, Any]:
    if len({grant.profile_id for grant in data.grants}) != len(data.grants):
        raise Conflict("setup must name each profile once")
    gateway = tools.gateway
    with gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        source: dict[str, Any] | None
        if data.repository_alias is not None:
            from onefoundry.capability_setup_host import select

            source = select(gateway, tools.project_id, data)
        else:
            source = _source(connection, tools.project_id, data)
        profiles = [profile_access.snapshot(connection, tools.project_id, g.profile_id) for g in data.grants]
        if data.repository_alias is None and all(
            set(g.tool_ids) <= set(p["allowed_tools"]) for g, p in zip(data.grants, profiles, strict=True)
        ):
            raise Conflict("requested tools are already granted; inspect their runtime dependency")
        binding = {
            "kind": data.kind,
            "request": data.model_dump(mode="json"),
            "profiles": profiles,
            "source": source,
            "session_id": str(tools.session_id),
            "work_order_id": str(scope["root_work_order_id"]),
        }
        key = uuid5(NAMESPACE_URL, "onefoundry:setup:" + digest(binding))
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='capability_setup_proposed' "
                "AND payload->>'setup_key'=:key LIMIT 1"
            ),
            {"p": tools.project_id, "key": str(key)},
        )
        if prior is not None:
            return dict(prior)
        effects = [
            f"{profile['name']}: allow {', '.join(grant.tool_ids)}."
            for grant, profile in zip(data.grants, profiles, strict=True)
        ]
        if data.repository_alias is not None:
            effects.append(
                f"Mount the configured host repository {data.repository_alias} read-only at the selected "
                "commit, register it in this project, and verify the requested file. "
                "The host worker briefly restarts the app after active work drains."
            )
        elif source is not None:
            effects.append(
                "Repository permission covers registered sources in this project. "
                f"Verify {source['display_name']} at its selected commit using {data.verification_path}."
            )
        content = "\n".join(
            [data.reason, *effects, "After approval, verify access and continue the original task."]
        )
        approval_id = uuid4()
        target = "Enable tools needed for this task"
        proposal_digest = digest({"binding": binding, "target": target, "content": content})
        connection.execute(
            text(
                "INSERT INTO approval_requests "
                "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                "proposal_digest,status) "
                "VALUES (:id,:p,:actor,:order,:run,'other',:target,:content,:digest,'pending')"
            ),
            {
                "id": approval_id,
                "p": tools.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "order": scope["root_work_order_id"],
                "run": tools.attempt_id,
                "target": target,
                "content": content,
                "digest": proposal_digest,
            },
        )
        value = {
            "tool_call_id": str(action_id),
            "setup_key": str(key),
            "approval_id": str(approval_id),
            "proposal_digest": proposal_digest,
            "binding": binding,
            "status": "waiting_for_operator",
        }
        gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "capability_setup_proposed",
            value,
        )
        gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "approval_requested",
            {
                "approval_id": str(approval_id),
                "action_type": "other",
                "proposal_digest": proposal_digest,
                "status": "pending",
            },
        )
        return value


def lock_decision_project(connection: Connection, project_id: UUID, approval_id: UUID) -> None:
    """Order setup decisions with other project-owned review requests."""
    bound = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:p AND event_type='capability_setup_proposed' "
            "AND payload->>'approval_id'=:id LIMIT 1"
        ),
        {"p": project_id, "id": str(approval_id)},
    )
    if bound:
        lock_project(connection, project_id)


def apply_decision(
    gateway: ToolGateway,
    connection: Connection,
    project_id: UUID,
    approval: dict[str, Any],
    decision: str,
) -> None:
    value = connection.scalar(
        text(
            "SELECT payload FROM events WHERE project_id=:p AND event_type='capability_setup_proposed' "
            "AND payload->>'approval_id'=:id LIMIT 1"
        ),
        {"p": project_id, "id": str(approval["id"])},
    )
    if value is None:
        return
    binding = value["binding"]
    if (
        digest({"binding": binding, "target": approval["target"], "content": approval["content"]})
        != approval["proposal_digest"]
    ):
        raise Conflict("setup proposal identity changed")
    lock_project(connection, project_id)
    data = CapabilitySetup.model_validate(binding["request"])
    if decision == "approved":
        selected: dict[str, Any] | None
        if data.repository_alias is not None:
            from onefoundry.capability_setup_host import select

            selected = select(gateway, project_id, data)
        else:
            selected = _source(connection, project_id, data)
        if selected != binding["source"]:
            raise Conflict("setup source changed; prepare a new proposal")
        for requested, expected in zip(data.grants, binding["profiles"], strict=True):
            profile_access.grant(connection, project_id, expected, requested.tool_ids)
    gateway._event(
        connection,
        project_id,
        UUID(binding["work_order_id"]),
        None,
        "capability_setup_decided",
        {"approval_id": str(approval["id"]), "decision": decision, "binding": binding},
    )


def status(gateway: ToolGateway, project_id: UUID, work_order_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT a.id,a.status,a.target,a.content,e.payload->'binding' AS binding, "
                    "(SELECT done.payload->'observation' FROM events done WHERE "
                    "done.project_id=e.project_id AND done.work_order_id=e.work_order_id "
                    "AND done.event_type='capability_setup_resumed' "
                    "AND done.payload->>'approval_id'=a.id::text "
                    "ORDER BY done.created_at DESC,done.id DESC LIMIT 1) AS observation "
                    "FROM events e JOIN approval_requests a ON a.project_id=e.project_id "
                    "AND a.id::text=e.payload->>'approval_id' "
                    "WHERE e.project_id=:p AND e.work_order_id=:w "
                    "AND e.event_type='capability_setup_proposed' "
                    "ORDER BY e.created_at DESC,e.id DESC LIMIT 50"
                ),
                {"p": project_id, "w": work_order_id},
            )
            .mappings()
            .all()
        )
    return [json.loads(json.dumps(dict(row), default=str)) for row in rows]
