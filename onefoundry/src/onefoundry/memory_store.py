"""SQL statements used only inside ToolGateway owned transactions.

No function here decides an agent action or writes an event. The gateway checks
state, scopes the project, and records each mutation with its event atomically.
"""

# Dynamic SQL below interpolates only module constants and generated bind names.
# ruff: noqa: S608

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.memory_policy import query_terms

if TYPE_CHECKING:
    from onefoundry.memory_embedding_settings import EmbeddingConfiguration

RESULT_COLUMNS = (
    "id, project_id, actor_id, run_id, summary, scope, source_kind, "
    "source_sales_draft_id, source_runner_job_id, source_capture_id, source_sha256, created_at"
)
CANDIDATE_COLUMNS = (
    "id, project_id, actor_id, source_result_id, summary, scope, status, "
    "created_at, reviewed_at, reviewed_by_actor_id, replaces_memory_id"
)
MEMORY_COLUMNS = (
    "id, project_id, actor_id, scope, text, source_result_id, source_candidate_id, created_at, "
    "state, superseded_by, changed_at, changed_by_actor_id"
)


def visibility_sql(alias: Literal["", "m"] = "") -> str:
    prefix = "m." if alias == "m" else ""
    return (
        f"((:scope = 'all' AND ({prefix}project_id = :project_id OR {prefix}scope = 'company')) OR "
        f"(:scope = 'project' AND {prefix}project_id = :project_id AND {prefix}scope = 'project') OR "
        f"(:scope = 'company' AND {prefix}scope = 'company'))"
    )


VISIBILITY_SQL = visibility_sql()


def list_results(connection: Connection, project_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            f"SELECT {RESULT_COLUMNS} FROM run_results WHERE project_id = :project_id "
            "ORDER BY created_at DESC, id DESC"
        ),
        {"project_id": project_id},
    ).mappings()
    return [dict(row) for row in rows]


def insert_result(
    connection: Connection,
    *,
    result_id: UUID,
    project_id: UUID,
    actor_id: str,
    run_id: UUID,
    summary: str,
    scope: str,
) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "INSERT INTO run_results (id, project_id, actor_id, run_id, summary, scope) "
                "VALUES (:id, :project_id, :actor_id, :run_id, :summary, :scope) "
                f"RETURNING {RESULT_COLUMNS}"
            ),
            {
                "id": result_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "summary": summary,
                "scope": scope,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_result(connection: Connection, project_id: UUID, result_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(f"SELECT {RESULT_COLUMNS} FROM run_results WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": result_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def get_result_by_run(connection: Connection, project_id: UUID, run_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(f"SELECT {RESULT_COLUMNS} FROM run_results WHERE project_id=:project_id AND run_id=:run_id"),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def insert_synthetic_result(
    connection: Connection,
    *,
    result_id: UUID,
    project_id: UUID,
    actor_id: str,
    run_id: UUID,
    summary: str,
    scope: str,
    source_kind: str,
    source_id: UUID,
    source_sha256: str,
) -> dict[str, Any]:
    if source_kind not in {"synthetic_sales", "synthetic_qa"}:
        raise ValueError("unsupported synthetic result source")
    sales_id = source_id if source_kind == "synthetic_sales" else None
    qa_id = source_id if source_kind == "synthetic_qa" else None
    row = (
        connection.execute(
            text(
                "INSERT INTO run_results (id,project_id,actor_id,run_id,summary,scope,source_kind,"
                "source_sales_draft_id,source_runner_job_id,source_sha256) "
                "VALUES (:id,:project_id,:actor_id,:run_id,:summary,:scope,:source_kind,"
                ":sales_id,:qa_id,:source_sha256) "
                f"RETURNING {RESULT_COLUMNS}"
            ),
            {
                "id": result_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "summary": summary,
                "scope": scope,
                "source_kind": source_kind,
                "sales_id": sales_id,
                "qa_id": qa_id,
                "source_sha256": source_sha256,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_candidate_by_result(
    connection: Connection, project_id: UUID, result_id: UUID
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                f"SELECT {CANDIDATE_COLUMNS} FROM dreamcycle_candidates "
                "WHERE project_id=:project_id AND source_result_id=:result_id"
            ),
            {"project_id": project_id, "result_id": result_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def list_candidates(connection: Connection, project_id: UUID, status: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"project_id": project_id}
    filter_sql = ""
    if status is not None:
        filter_sql = " AND status = :status"
        params["status"] = status
    rows = connection.execute(
        text(
            f"SELECT {CANDIDATE_COLUMNS}, (SELECT r.source_capture_id FROM run_results r "
            "WHERE r.id=dreamcycle_candidates.source_result_id) AS source_capture_id "
            "FROM dreamcycle_candidates "
            "WHERE project_id = :project_id" + filter_sql + " ORDER BY created_at DESC, id DESC"
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def insert_candidate(
    connection: Connection,
    *,
    candidate_id: UUID,
    project_id: UUID,
    actor_id: str,
    source_result_id: UUID,
    summary: str,
    scope: str,
    replaces_memory_id: UUID | None = None,
) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "INSERT INTO dreamcycle_candidates "
                "(id, project_id, actor_id, source_result_id, summary, scope, replaces_memory_id) "
                "VALUES (:id, :project_id, :actor_id, :source_result_id, :summary, :scope, "
                ":replaces) "
                f"RETURNING {CANDIDATE_COLUMNS}"
            ),
            {
                "id": candidate_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "source_result_id": source_result_id,
                "summary": summary,
                "scope": scope,
                "replaces": replaces_memory_id,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def get_candidate_for_update(
    connection: Connection,
    project_id: UUID,
    candidate_id: UUID,
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                f"SELECT {CANDIDATE_COLUMNS} FROM dreamcycle_candidates "
                "WHERE project_id = :project_id AND id = :id FOR UPDATE"
            ),
            {"project_id": project_id, "id": candidate_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def review_candidate(
    connection: Connection,
    project_id: UUID,
    candidate_id: UUID,
    actor_id: str,
    decision: str,
) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "UPDATE dreamcycle_candidates SET status = :decision, "
                "reviewed_at = clock_timestamp(), reviewed_by_actor_id = :actor_id "
                "WHERE project_id = :project_id AND id = :id "
                f"RETURNING {CANDIDATE_COLUMNS}"
            ),
            {"project_id": project_id, "id": candidate_id, "actor_id": actor_id, "decision": decision},
        )
        .mappings()
        .one()
    )
    return dict(row)


def publish_memory(
    connection: Connection,
    *,
    memory_id: UUID,
    project_id: UUID,
    actor_id: str,
    scope: str,
    summary: str,
    result_id: UUID,
    candidate_id: UUID,
) -> None:
    connection.execute(
        text(
            "INSERT INTO memory_items "
            "(id, project_id, actor_id, scope, text, source_result_id, source_candidate_id) "
            "VALUES (:id, :project_id, :actor_id, :scope, :text, :source_result_id, "
            ":source_candidate_id)"
        ),
        {
            "id": memory_id,
            "project_id": project_id,
            "actor_id": actor_id,
            "scope": scope,
            "text": summary,
            "source_result_id": result_id,
            "source_candidate_id": candidate_id,
        },
    )


def retrieve_memory(
    connection: Connection,
    project_id: UUID,
    query: str,
    limit: int,
    scope: str | None,
    settings: "EmbeddingConfiguration | None" = None,
) -> list[dict[str, Any]]:
    query_terms(query)  # Validate the same input bound used at the HTTP boundary.
    if not 1 <= limit <= 50 or scope not in {None, "project", "company"}:
        raise ValueError("invalid memory limit or scope")
    if query.strip() and settings is not None and settings.memory_search_mode == "hybrid":
        from onefoundry.memory_vector_search import retrieve

        return retrieve(connection, project_id, query, limit, scope, settings)
    params: dict[str, Any] = {"project_id": project_id, "limit": limit, "scope": scope or "all"}
    if not query.strip():
        rows = connection.execute(
            text(
                f"SELECT {MEMORY_COLUMNS} FROM memory_items "
                f"WHERE state = 'current' AND {VISIBILITY_SQL} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        ).mappings()
        return [{**dict(row), "score": 0} for row in rows]

    # Score exact words before LIMIT, so substring matches cannot crowd out
    # older exact matches. No raw-text index or event payload is created.
    params["query"] = query
    rows = connection.execute(
        text(
            "WITH query_terms AS ("
            "SELECT word FROM unnest(regexp_split_to_array(lower(:query), "
            "'[^[:alnum:]_]+')) WITH ORDINALITY AS token(word, position) "
            "WHERE word <> '' GROUP BY word ORDER BY min(position) LIMIT 10), "
            f"scored AS (SELECT {MEMORY_COLUMNS}, "
            "(SELECT count(*) FROM query_terms "
            "WHERE word = ANY(regexp_split_to_array(lower(memory_items.text), "
            "'[^[:alnum:]_]+')))::integer AS score "
            f"FROM memory_items WHERE state = 'current' AND {VISIBILITY_SQL}) "
            "SELECT * FROM scored WHERE score > 0 "
            "ORDER BY score DESC, created_at DESC, id DESC LIMIT :limit"
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]
