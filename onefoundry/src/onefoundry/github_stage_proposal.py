"""Exact, local-only approval proposal for a future GitHub source stage."""

# The only SQL interpolation below is the module-constant approval column list.
# ruff: noqa: S608

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.engine import RowMapping

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.source_github import BRANCH, REPO_PART, SHA
from onefoundry.tool_gateway import APPROVAL_COLUMNS

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def propose(gateway: "ToolGateway", project_id: UUID, run_id: UUID, source_id: UUID) -> dict[str, Any]:
    """Record one exact proposed target; this has no fetch or credential side effect."""
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
            raise Conflict("GitHub stage proposal requires a running manual run")
        gateway._require_unblocked_order(connection, project_id, run["work_order_id"])
        source = (
            connection.execute(
                text(
                    "SELECT s.kind,s.account_id,s.owner,s.repo,s.branch,s.revision "
                    "FROM project_sources s JOIN github_accounts a ON "
                    "a.project_id=s.project_id AND a.id=s.account_id "
                    "WHERE s.project_id=:project_id AND s.id=:source_id FOR KEY SHARE OF s"
                ),
                {"project_id": project_id, "source_id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise NotFound("saved GitHub source not found")
        if (
            source["kind"] != "github"
            or not REPO_PART.fullmatch(source["owner"])
            or not REPO_PART.fullmatch(source["repo"])
            or not BRANCH.fullmatch(source["branch"])
            or not SHA.fullmatch(source["revision"])
        ):
            raise Conflict("saved GitHub source metadata is invalid")
        duplicate = connection.scalar(
            text(
                "SELECT 1 FROM approval_requests a JOIN events e ON "
                "e.project_id=a.project_id AND e.event_type='github_stage_proposed' "
                "AND e.payload->>'approval_id'=a.id::text "
                "WHERE a.project_id=:project_id AND a.run_id=:run_id "
                "AND e.payload->>'source_id'=:source_id "
                "AND a.status IN ('pending','approved') LIMIT 1"
            ),
            {"project_id": project_id, "run_id": run_id, "source_id": str(source_id)},
        )
        if duplicate is not None:
            raise Conflict("GitHub staging proposal already pending or approved for this run and source")
        exact = {
            "kind": "github_stage_v1",
            "project_id": str(project_id),
            "source_id": str(source_id),
            "account_id": str(source["account_id"]),
            "owner": source["owner"],
            "repo": source["repo"],
            "branch": source["branch"],
            "saved_revision": source["revision"],
        }
        content = json.dumps(exact, sort_keys=True, separators=(",", ":"))
        target = f"{source['owner']}/{source['repo']}:{source['branch']}@{source['revision']}"
        proposal = {"action_type": "other", "target": target, "content": content}
        digest = hashlib.sha256(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        approval_id = uuid4()
        row = (
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                    "proposal_digest,status) VALUES "
                    "(:id,:project_id,:actor_id,:work_order_id,:run_id,'other',:target,:content,"
                    ":digest,'pending') "
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
                    "digest": digest,
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
            "github_stage_proposed",
            {
                "approval_id": str(approval_id),
                "proposal_digest": digest,
                "source_id": str(source_id),
                "saved_revision": source["revision"],
                "stage_status": "not_launched",
            },
        )
        return {
            "approval": {**dict(row), "github_stage": True},
            "source": exact,
            "stage_status": "not_launched",
        }


def bound_source_state(
    connection: Connection, project_id: UUID, approval: RowMapping, *, lock: bool
) -> tuple[dict[str, str], bool] | None:
    """Return exact saved identity and currentness for a schema-backed stage approval.

    With lock=True, source deletion or project retirement cannot race the
    approving transaction. A generic approval with lookalike content returns None.
    """
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_stage_proposed' AND payload->>'approval_id'=:id "
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
        exact = json.loads(approval["content"])
        if not isinstance(exact, dict) or set(exact) != {
            "kind",
            "project_id",
            "source_id",
            "account_id",
            "owner",
            "repo",
            "branch",
            "saved_revision",
        }:
            raise ValueError("proposal shape mismatch")
        if exact["kind"] != "github_stage_v1" or exact["project_id"] != str(project_id):
            raise ValueError("proposal kind or project mismatch")
        source_id = UUID(exact["source_id"])
        account_id = UUID(exact["account_id"])
        if (
            approval["action_type"] != "other"
            or not REPO_PART.fullmatch(exact["owner"])
            or not REPO_PART.fullmatch(exact["repo"])
            or not BRANCH.fullmatch(exact["branch"])
            or not SHA.fullmatch(exact["saved_revision"])
        ):
            raise ValueError("proposal target invalid")
        target = f"{exact['owner']}/{exact['repo']}:{exact['branch']}@{exact['saved_revision']}"
        if approval["target"] != target:
            raise ValueError("proposal target mismatch")
        proposal = {"action_type": "other", "target": target, "content": approval["content"]}
        digest = hashlib.sha256(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != approval["proposal_digest"]:
            raise ValueError("proposal digest mismatch")
    except (KeyError, TypeError, ValueError):
        raise Conflict("GitHub stage proposal binding is invalid") from None
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
    current = (
        connection.execute(text(source_query), {"project_id": project_id, "source_id": source_id})
        .mappings()
        .one_or_none()
    )
    fresh = (
        active == "active"
        and current is not None
        and all(
            current[key] == expected
            for key, expected in (
                ("account_id", account_id),
                ("owner", exact["owner"]),
                ("repo", exact["repo"]),
                ("branch", exact["branch"]),
                ("revision", exact["saved_revision"]),
            )
        )
    )
    return exact, fresh


def status(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    """Recheck saved owner state before describing an approved proposal as usable."""
    with gateway.engine.connect() as connection:
        approval = (
            connection.execute(
                text(
                    "SELECT id,run_id,status,action_type,target,content,proposal_digest FROM "
                    "approval_requests WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if approval is None:
            raise NotFound("GitHub stage proposal not found")
        bound = bound_source_state(connection, project_id, approval, lock=False)
        if bound is None:
            raise NotFound("GitHub stage proposal not found")
        exact, fresh = bound
        return {
            "approval_id": approval_id,
            "decision": approval["status"],
            "source": exact,
            "source_current": fresh,
            "stage_status": "not_launched",
        }
