"""Bounded model-facing projections of project records.

The owning tables remain with their existing stores. These queries select only
the fields a role may receive; artifact bytes/paths and event payloads stay out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import Connection, text

from onefoundry import memory_store
from onefoundry.memory_policy import query_terms
from onefoundry.workflow_schemas import RunStatus, StrictModel, WorkOrderStatus, local_text

if TYPE_CHECKING:
    from onefoundry.memory_embedding_settings import EmbeddingConfiguration
    from onefoundry.role_tool_admission import RunContext

EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
RUN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"run_id": {"type": "string", "format": "uuid"}},
    "required": ["run_id"],
    "additionalProperties": False,
}
MEMORY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 120}},
    "required": ["query"],
    "additionalProperties": False,
}


class EmptyArguments(StrictModel):
    pass


class RunArguments(StrictModel):
    run_id: UUID


class MemoryArguments(StrictModel):
    query: str = Field(max_length=120, strict=True)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        query = local_text(value, allow_empty=True)
        query_terms(query)
        return query


def parse_empty(arguments: object) -> dict[str, Any]:
    return EmptyArguments.model_validate(arguments).model_dump(mode="json")


def parse_run(arguments: object) -> dict[str, Any]:
    return RunArguments.model_validate(arguments).model_dump(mode="json")


def parse_memory(arguments: object) -> dict[str, Any]:
    return MemoryArguments.model_validate(arguments).model_dump(mode="json")


class ProjectBrief(StrictModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=160)
    goal: str = Field(max_length=4000)
    desired_outcome: str = Field(max_length=4000)


class RecentWorkItem(StrictModel):
    work_order_id: UUID
    title: str = Field(min_length=1, max_length=160)
    status: WorkOrderStatus
    run_id: UUID | None
    run_status: RunStatus | None


class RecentWork(StrictModel):
    project_id: UUID
    items: list[RecentWorkItem]
    has_more: bool


class ResultEvidence(StrictModel):
    result_id: UUID
    summary: str = Field(min_length=1, max_length=4096)
    scope: Literal["project", "company"]
    source_kind: str = Field(min_length=1, max_length=64)


class ArtifactEvidence(StrictModel):
    artifact_id: UUID
    source: Literal["synthetic", "external"]
    kind: str = Field(min_length=1, max_length=32)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0, le=65536)


class EventEvidence(StrictModel):
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=80)
    created_at: str


class RunEvidence(StrictModel):
    run_id: UUID
    found: bool
    work_order_id: UUID | None
    work_order_title: str | None
    status: RunStatus | None
    mode: str | None
    result: ResultEvidence | None
    artifacts: list[ArtifactEvidence]
    artifacts_has_more: bool
    events: list[EventEvidence]
    events_has_more: bool


class ApprovedMemoryItem(StrictModel):
    memory_id: UUID
    scope: Literal["project", "company"]
    text: str = Field(min_length=1, max_length=4096)
    source_result_id: UUID
    score: int = Field(ge=0)


class ApprovedMemory(StrictModel):
    project_id: UUID
    items: list[ApprovedMemoryItem]
    has_more: bool


def validate_project_brief(result: dict[str, Any]) -> None:
    ProjectBrief.model_validate(result)


def validate_recent_project_work(result: dict[str, Any]) -> None:
    RecentWork.model_validate(result)


def validate_run_evidence(result: dict[str, Any]) -> None:
    RunEvidence.model_validate(result)


def validate_approved_memory(result: dict[str, Any]) -> None:
    ApprovedMemory.model_validate(result)


def project_brief(connection: Connection, context: RunContext, _arguments: dict[str, Any]) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT name,goal,desired_outcome FROM projects WHERE id=:project_id"),
            {"project_id": context.project_id},
        )
        .mappings()
        .one()
    )
    return {
        "project_id": str(context.project_id),
        "name": row["name"],
        "goal": row["goal"],
        "desired_outcome": row["desired_outcome"],
    }


def recent_project_work(
    connection: Connection, context: RunContext, _arguments: dict[str, Any]
) -> dict[str, Any]:
    rows = (
        connection.execute(
            text(
                "SELECT w.id AS work_order_id,w.title,w.status,r.id AS run_id,r.status AS run_status "
                "FROM work_orders w LEFT JOIN LATERAL ("
                "SELECT id,status FROM runs WHERE project_id=w.project_id "
                "AND work_order_id=w.id ORDER BY started_at DESC,id DESC LIMIT 1"
                ") r ON true "
                "WHERE w.project_id=:project_id ORDER BY w.created_at DESC,w.id DESC LIMIT :limit"
            ),
            {"project_id": context.project_id, "limit": context.max_items + 1},
        )
        .mappings()
        .all()
    )
    return {
        "project_id": str(context.project_id),
        "items": [
            {
                "work_order_id": str(row["work_order_id"]),
                "title": row["title"],
                "status": row["status"],
                "run_id": str(row["run_id"]) if row["run_id"] else None,
                "run_status": row["run_status"],
            }
            for row in rows[: context.max_items]
        ],
        "has_more": len(rows) > context.max_items,
    }


def run_evidence(connection: Connection, context: RunContext, arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = UUID(arguments["run_id"])
    base: dict[str, Any] = {
        "run_id": str(run_id),
        "found": False,
        "work_order_id": None,
        "work_order_title": None,
        "status": None,
        "mode": None,
        "result": None,
        "artifacts": [],
        "artifacts_has_more": False,
        "events": [],
        "events_has_more": False,
    }
    run = (
        connection.execute(
            text(
                "SELECT r.work_order_id,r.status,r.mode,w.title FROM runs r "
                "JOIN work_orders w ON w.project_id=r.project_id AND w.id=r.work_order_id "
                "WHERE r.project_id=:project_id AND r.id=:run_id"
            ),
            {"project_id": context.project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if run is None:
        return base
    base.update(
        found=True,
        work_order_id=str(run["work_order_id"]),
        work_order_title=run["title"],
        status=run["status"],
        mode=run["mode"],
    )
    result = (
        connection.execute(
            text(
                "SELECT id,summary,scope,source_kind FROM run_results "
                "WHERE project_id=:project_id AND run_id=:run_id"
            ),
            {"project_id": context.project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if result is not None:
        base["result"] = {
            "result_id": str(result["id"]),
            "summary": result["summary"],
            "scope": result["scope"],
            "source_kind": result["source_kind"],
        }
    artifacts = (
        connection.execute(
            text(
                "SELECT id,source,kind,sha256,byte_size,created_at FROM ("
                "SELECT id,'synthetic' AS source,kind,sha256,byte_size,created_at "
                "FROM runner_artifacts WHERE project_id=:project_id AND run_id=:run_id "
                "UNION ALL "
                "SELECT id,'external' AS source,kind,sha256,byte_size,created_at "
                "FROM external_code_artifacts WHERE project_id=:project_id AND run_id=:run_id"
                ") a ORDER BY created_at DESC,id DESC LIMIT :limit"
            ),
            {"project_id": context.project_id, "run_id": run_id, "limit": context.max_items + 1},
        )
        .mappings()
        .all()
    )
    base["artifacts"] = [
        {
            "artifact_id": str(row["id"]),
            "source": row["source"],
            "kind": row["kind"],
            "sha256": row["sha256"],
            "byte_size": row["byte_size"],
        }
        for row in artifacts[: context.max_items]
    ]
    base["artifacts_has_more"] = len(artifacts) > context.max_items
    events = (
        connection.execute(
            text(
                "SELECT id,event_type,created_at FROM events "
                "WHERE project_id=:project_id AND run_id=:run_id "
                "ORDER BY created_at DESC,id DESC LIMIT :limit"
            ),
            {"project_id": context.project_id, "run_id": run_id, "limit": context.max_items + 1},
        )
        .mappings()
        .all()
    )
    base["events"] = [
        {
            "event_id": str(row["id"]),
            "event_type": row["event_type"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in events[: context.max_items]
    ]
    base["events_has_more"] = len(events) > context.max_items
    return base


def approved_memory(
    connection: Connection,
    context: RunContext,
    arguments: dict[str, Any],
    settings: EmbeddingConfiguration | None = None,
) -> dict[str, Any]:
    scope = "project" if "project" in (context.snapshot_memory_scope, context.current_memory_scope) else None
    rows = memory_store.retrieve_memory(
        connection, context.project_id, arguments["query"], context.max_items + 1, scope, settings
    )
    return {
        "project_id": str(context.project_id),
        "items": [
            {
                "memory_id": str(row["id"]),
                "scope": row["scope"],
                "text": row["text"],
                "source_result_id": str(row["source_result_id"]),
                "score": row["score"],
            }
            for row in rows[: context.max_items]
        ],
        "has_more": len(rows) > context.max_items,
    }
