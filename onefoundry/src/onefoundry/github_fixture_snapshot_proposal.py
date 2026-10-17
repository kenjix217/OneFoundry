"""Tool Gateway approval binding for durable fake HTTPS source snapshots."""

# SQL fragments are constants; request values are bound.
# ruff: noqa: S608

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.engine import RowMapping

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.github_fixture_snapshot_contract import FetchIntent, FetchSource, expiry
from onefoundry.github_stage_contract import canonical, digest
from onefoundry.tool_gateway import APPROVAL_COLUMNS

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _target(intent: FetchIntent) -> str:
    return f"{intent.owner}/{intent.repo}:{intent.branch}@{intent.saved_revision}"


def _digest(content: str, target: str) -> str:
    return digest({"action_type": "other", "target": target, "content": content})


def propose(
    gateway: "ToolGateway", project_id: UUID, run_id: UUID, source_id: UUID, idempotency_key: UUID
) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        run = (
            connection.execute(
                text(
                    "SELECT work_order_id,status,mode FROM runs WHERE project_id=:project_id "
                    "AND id=:run_id FOR UPDATE"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("run not found")
        if run["status"] != "running" or run["mode"] != "manual_planning":
            raise Conflict("fixture fetch proposal requires a running manual run")
        gateway._require_unblocked_order(connection, project_id, run["work_order_id"])
        source = (
            connection.execute(
                text(
                    "SELECT s.account_id,s.owner,s.repo,s.branch,s.revision FROM project_sources s "
                    "JOIN github_accounts a ON a.project_id=s.project_id AND a.id=s.account_id "
                    "WHERE s.project_id=:project_id AND s.id=:source_id AND s.kind='github' "
                    "FOR KEY SHARE OF s"
                ),
                {"project_id": project_id, "source_id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise NotFound("saved GitHub source not found")
        exact = {
            "kind": "github_fixture_snapshot_v3",
            "project_id": str(project_id),
            "source_id": str(source_id),
            "account_id": str(source["account_id"]),
            "owner": source["owner"],
            "repo": source["repo"],
            "branch": source["branch"],
            "saved_revision": source["revision"],
        }
        try:
            FetchSource.model_validate(exact)
        except ValueError:
            raise Conflict("saved GitHub source metadata is invalid") from None
        previous = (
            connection.execute(
                text(
                    "SELECT a.id,a.project_id,a.work_order_id,a.run_id,a.action_type,a.target,"
                    "a.content,a.proposal_digest,a.status,a.created_at,a.decided_at "
                    "FROM approval_requests a JOIN events e ON "
                    "e.project_id=a.project_id AND e.run_id=a.run_id "
                    "AND e.event_type='github_fixture_snapshot_proposed' "
                    "AND e.payload->>'approval_id'=a.id::text "
                    "WHERE a.project_id=:project_id AND a.run_id=:run_id "
                    "AND e.payload->>'idempotency_key'=:key LIMIT 1"
                ),
                {"project_id": project_id, "run_id": run_id, "key": str(idempotency_key)},
            )
            .mappings()
            .one_or_none()
        )
        if previous is not None:
            prior = FetchIntent.model_validate_json(previous["content"])
            if prior.model_dump(include=set(FetchSource.model_fields)) != exact:
                raise Conflict("fixture fetch idempotency key already bound to a different source")
            if datetime.now(UTC) >= expiry(prior.expires_at):
                raise Conflict("fixture fetch intent expired; start a new proposal")
            return {
                "approval": {**dict(previous), "github_snapshot": True},
                "source": exact,
                "snapshot_status": "not_launched",
                "expires_at": prior.expires_at,
            }
        outstanding = connection.execute(
            text(
                "SELECT a.content FROM approval_requests a JOIN events e ON "
                "e.project_id=a.project_id AND e.run_id=a.run_id "
                "AND e.event_type='github_fixture_snapshot_proposed' "
                "AND e.payload->>'approval_id'=a.id::text "
                "WHERE a.project_id=:project_id AND a.run_id=:run_id "
                "AND e.payload->>'source_id'=:source_id "
                "AND a.status IN ('pending','approved')"
            ),
            {"project_id": project_id, "run_id": run_id, "source_id": str(source_id)},
        ).mappings()
        if any(
            datetime.now(UTC) < expiry(FetchIntent.model_validate_json(row["content"]).expires_at)
            for row in outstanding
        ):
            raise Conflict("fixture fetch proposal already pending or approved for this source")
        intent = FetchIntent.model_validate(
            {
                **exact,
                "action": "materialize_local_https_fixture_snapshot",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                "idempotency_key": str(idempotency_key),
            }
        )
        content = canonical(intent.model_dump()).decode()
        target = _target(intent)
        approval_id = uuid4()
        proposal_digest = _digest(content, target)
        row = (
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                    "proposal_digest,status) VALUES (:id,:project_id,:actor_id,:work_order_id,"
                    ":run_id,'other',:target,:content,:digest,'pending') "
                    f"RETURNING {APPROVAL_COLUMNS}"
                ),
                {
                    "id": approval_id,
                    "project_id": project_id,
                    "actor_id": gateway.actor_id,
                    "work_order_id": run["work_order_id"],
                    "run_id": run_id,
                    "target": target,
                    "content": content,
                    "digest": proposal_digest,
                },
            )
            .mappings()
            .one()
        )
        gateway._event(
            connection,
            project_id,
            run["work_order_id"],
            run_id,
            "github_fixture_snapshot_proposed",
            {
                "approval_id": str(approval_id),
                "proposal_digest": proposal_digest,
                "source_id": str(source_id),
                "idempotency_key": str(idempotency_key),
                "expires_at": intent.expires_at,
                "action": intent.action,
            },
        )
        return {
            "approval": {**dict(row), "github_snapshot": True},
            "source": exact,
            "snapshot_status": "not_launched",
            "expires_at": intent.expires_at,
        }


def bound_intent(
    connection: Connection, project_id: UUID, approval: RowMapping, *, lock: bool
) -> tuple[FetchIntent, bool] | None:
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_fixture_snapshot_proposed' AND payload->>'approval_id'=:id "
            "AND payload->>'proposal_digest'=:digest"
        ),
        {
            "project_id": project_id,
            "run_id": approval["run_id"],
            "id": str(approval["id"]),
            "digest": approval["proposal_digest"],
        },
    )
    if marker is None:
        return None
    try:
        intent = FetchIntent.model_validate_json(approval["content"])
        if (
            intent.project_id != str(project_id)
            or approval["action_type"] != "other"
            or approval["target"] != _target(intent)
            or approval["proposal_digest"] != _digest(approval["content"], approval["target"])
        ):
            raise ValueError("intent binding mismatch")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise Conflict("fixture fetch intent binding is invalid") from None
    project_query = "SELECT status FROM projects WHERE id=:id"
    source_query = (
        "SELECT s.account_id,s.owner,s.repo,s.branch,s.revision FROM project_sources s "
        "JOIN github_accounts a ON a.project_id=s.project_id AND a.id=s.account_id "
        "WHERE s.project_id=:project_id AND s.id=:source_id AND s.kind='github'"
    )
    if lock:
        project_query += " FOR SHARE"
        source_query += " FOR KEY SHARE OF s"
    active = connection.scalar(text(project_query), {"id": project_id})
    source = (
        connection.execute(
            text(source_query), {"project_id": project_id, "source_id": UUID(intent.source_id)}
        )
        .mappings()
        .one_or_none()
    )
    fresh = (
        active == "active"
        and source is not None
        and all(
            source[key] == value
            for key, value in (
                ("account_id", UUID(intent.account_id)),
                ("owner", intent.owner),
                ("repo", intent.repo),
                ("branch", intent.branch),
                ("revision", intent.saved_revision),
            )
        )
    )
    return intent, fresh


def status(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        approval = (
            connection.execute(
                text(
                    "SELECT id,run_id,status,action_type,target,content,proposal_digest "
                    "FROM approval_requests WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if approval is None:
            raise NotFound("fixture fetch proposal not found")
        bound = bound_intent(connection, project_id, approval, lock=False)
        if bound is None:
            raise NotFound("fixture fetch proposal not found")
        intent, fresh = bound
        return {
            "approval_id": approval_id,
            "decision": approval["status"],
            "source": intent.model_dump(include=set(FetchSource.model_fields)),
            "source_current": fresh,
            "snapshot_status": "not_launched",
            "expires_at": intent.expires_at,
            "expired": datetime.now(UTC) >= expiry(intent.expires_at),
            "action": intent.action,
            "idempotency_key": intent.idempotency_key,
        }
