"""Scope-filtered exact cosine search of derived vectors joined to current facts."""

# Interpolated fields and aliases are module-owned constants; all caller values are binds.
# ruff: noqa: S608

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Unavailable
from onefoundry.memory_embedding_settings import EmbeddingConfiguration
from onefoundry.memory_embeddings import LocalEmbeddings, model_key
from onefoundry.memory_store import MEMORY_COLUMNS, visibility_sql


def retrieve(
    connection: Connection,
    project_id: UUID,
    query: str,
    limit: int,
    scope: str | None,
    settings: EmbeddingConfiguration,
) -> list[dict[str, Any]]:
    # Bound even the ordinary operator API, without relaxing a narrower gateway deadline.
    connection.execute(
        text(
            "SELECT set_config('statement_timeout', CASE WHEN current_setting('statement_timeout')='0' "
            "THEN CAST(:deadline AS text) ELSE CAST(least(:deadline, "
            "extract(epoch FROM current_setting('statement_timeout')::interval)*1000)::bigint AS text) "
            "END, true)"
        ),
        {"deadline": settings.memory_embedding_timeout_seconds * 1000},
    )
    params: dict[str, Any] = {
        "project_id": project_id,
        "scope": scope or "all",
        "limit": limit,
        "model_key": model_key(settings),
        "dims": settings.memory_embedding_dimensions,
    }
    # Refuse incomplete indexes rather than silently losing approved relevant facts.
    visible = connection.execute(
        text(
            f"SELECT count(*),count(e.memory_id) FROM memory_items m "
            "LEFT JOIN memory_embeddings e ON e.memory_id=m.id AND e.project_id=m.project_id "
            "AND e.model_key=:model_key AND e.dimensions=:dims "
            "AND e.source_sha256=encode(sha256(convert_to(m.text,'UTF8')),'hex') "
            f"WHERE m.state='current' AND {visibility_sql('m')}",
        ),
        params,
    ).one()
    if visible[0] != visible[1]:
        raise Unavailable("memory_embedding_index_incomplete")
    if visible[0] == 0:
        return []
    params.update(
        vector=LocalEmbeddings(settings).embed([query], "query")[0],
        query=query,
        threshold=settings.memory_embedding_min_similarity,
    )
    columns = ", ".join("m." + column.strip() for column in MEMORY_COLUMNS.split(","))
    visibility = visibility_sql("m")
    rows = connection.execute(
        text(
            "WITH words AS (SELECT word FROM unnest(regexp_split_to_array(lower(:query), "
            "'[^[:alnum:]_]+')) WITH ORDINALITY token(word,pos) WHERE word<>'' "
            "GROUP BY word ORDER BY min(pos) LIMIT 10), "
            f"visible AS (SELECT m.*,e.embedding FROM memory_items m LEFT JOIN memory_embeddings e "
            "ON e.memory_id=m.id AND e.project_id=m.project_id AND e.model_key=:model_key "
            "AND e.dimensions=:dims AND e.source_sha256=encode(sha256(convert_to(m.text,'UTF8')),'hex') "
            f"WHERE m.state='current' AND {visibility}), "
            "coverage AS (SELECT count(*)<>count(embedding) AS incomplete FROM visible), "
            f"ranked AS (SELECT {columns}, "
            "(SELECT count(*) FROM words WHERE word=ANY(regexp_split_to_array(lower(m.text), "
            "'[^[:alnum:]_]+')))::integer AS score, "
            "(SELECT sum(v.value*q.value) FROM unnest(m.embedding) WITH ORDINALITY v(value,pos) "
            "JOIN unnest(CAST(:vector AS double precision[])) WITH ORDINALITY q(value,pos) "
            "ON v.pos=q.pos) AS similarity FROM visible m WHERE m.embedding IS NOT NULL), "
            "selected AS (SELECT * FROM ranked WHERE score>0 OR similarity>=:threshold "
            "ORDER BY score DESC,similarity DESC,created_at DESC,id DESC LIMIT :limit) "
            "SELECT selected.*,coverage.incomplete FROM coverage LEFT JOIN selected ON true "
            "ORDER BY score DESC,similarity DESC,created_at DESC,id DESC"
        ),
        params,
    ).mappings()
    results = []
    for row in rows:
        if row["incomplete"]:
            raise Unavailable("memory_embedding_index_incomplete")
        if row["id"] is not None:
            result = dict(row)
            del result["incomplete"]
            results.append(result)
    return results
