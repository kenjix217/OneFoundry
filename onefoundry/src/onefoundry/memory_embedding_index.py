"""Atomic publication indexing and explicit bounded rebuilds through the Tool Gateway."""

import argparse
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, create_engine, text

from onefoundry.gateway_errors import Unavailable
from onefoundry.memory_embeddings import LocalEmbeddings, model_key, text_hash
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway


def _store_vector(
    connection: Connection,
    gateway: ToolGateway,
    settings: Settings,
    project_id: UUID,
    memory_id: UUID,
    summary: str,
    vector: list[float],
) -> None:
    connection.execute(
        text(
            "INSERT INTO memory_embeddings "
            "(memory_id,model_key,project_id,actor_id,model,source_sha256,dimensions,embedding) "
            "VALUES (:id,:key,:p,:actor,:model,:hash,:dims,:vector) "
            "ON CONFLICT (memory_id,model_key) DO UPDATE SET actor_id=EXCLUDED.actor_id,"
            "model=EXCLUDED.model,source_sha256=EXCLUDED.source_sha256,"
            "dimensions=EXCLUDED.dimensions,embedding=EXCLUDED.embedding,created_at=now()"
        ),
        {
            "id": memory_id,
            "p": project_id,
            "actor": gateway.actor_id,
            "key": model_key(settings),
            "model": settings.memory_embedding_model,
            "hash": text_hash(summary),
            "dims": settings.memory_embedding_dimensions,
            "vector": vector,
        },
    )


def index_publication(
    gateway: ToolGateway,
    connection: Connection,
    project_id: UUID,
    memory_id: UUID,
    summary: str,
) -> None:
    """Make a newly approved/restored fact searchable in its publication transaction."""
    settings = gateway.live_text_settings
    if settings is None or settings.memory_search_mode != "hybrid":
        return
    key = model_key(settings)
    if connection.scalar(
        text(
            "SELECT 1 FROM memory_embeddings WHERE project_id=:p AND memory_id=:id "
            "AND model_key=:key AND source_sha256=:hash AND dimensions=:dims"
        ),
        {
            "p": project_id,
            "id": memory_id,
            "key": key,
            "hash": text_hash(summary),
            "dims": settings.memory_embedding_dimensions,
        },
    ):
        return
    if contains_sensitive_text(summary):
        raise Unavailable("memory_embedding_content_unavailable")
    vector = LocalEmbeddings(settings).embed([summary], "document")[0]
    _store_vector(connection, gateway, settings, project_id, memory_id, summary, vector)
    gateway._event(
        connection,
        project_id,
        None,
        None,
        "memory_embeddings_indexed",
        {"model_key": key, "indexed": 1, "skipped_changed": 0},
    )


def index_project(
    gateway: ToolGateway, settings: Settings, project_id: UUID, *, limit: int
) -> dict[str, int]:
    if settings.memory_search_mode != "hybrid" or not 1 <= limit <= 16:
        raise ValueError("enable validated hybrid configuration and select a batch of 1–16 facts")
    key = model_key(settings)
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        rows = list(
            connection.execute(
                text(
                    "SELECT m.id,m.text FROM memory_items m LEFT JOIN memory_embeddings e "
                    "ON e.memory_id=m.id AND e.project_id=m.project_id AND e.model_key=:key "
                    "AND e.source_sha256=encode(sha256(convert_to(m.text,'UTF8')),'hex') "
                    "AND e.dimensions=:dims WHERE m.project_id=:p AND m.state='current' "
                    "AND e.memory_id IS NULL ORDER BY m.created_at,m.id LIMIT :limit"
                ),
                {"p": project_id, "key": key, "limit": limit, "dims": settings.memory_embedding_dimensions},
            ).mappings()
        )
    if any(contains_sensitive_text(str(row["text"])) for row in rows):
        raise Unavailable("memory_embedding_content_unavailable")
    if not rows:
        return {"indexed": 0, "skipped_changed": 0}
    vectors = LocalEmbeddings(settings).embed([str(row["text"]) for row in rows], "document")
    indexed = skipped = 0
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        for row, vector in zip(rows, vectors, strict=True):
            current = connection.execute(
                text(
                    "SELECT text FROM memory_items WHERE project_id=:p AND id=:id "
                    "AND state='current' FOR SHARE"
                ),
                {"p": project_id, "id": row["id"]},
            ).scalar_one_or_none()
            if not isinstance(current, str) or current != row["text"]:
                skipped += 1
                continue
            _store_vector(connection, gateway, settings, project_id, row["id"], current, vector)
            indexed += 1
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "memory_embeddings_indexed",
            {"model_key": key, "indexed": indexed, "skipped_changed": skipped},
        )
    return {"indexed": indexed, "skipped_changed": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index an explicitly selected project's approved current facts"
    )
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--limit", type=int, default=16)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        settings = Settings()
        engine = create_engine(
            settings.database_url,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.memory_embedding_timeout_seconds,
                "options": (
                    f"-c statement_timeout={settings.memory_embedding_timeout_seconds * 1000} "
                    f"-c lock_timeout={settings.memory_embedding_timeout_seconds * 1000}"
                ),
            },
        )
        gateway = ToolGateway(engine, settings.operator_actor_id, live_text_settings=settings)
        result: dict[str, Any] = index_project(gateway, settings, args.project_id, limit=args.limit)
    except Exception:
        raise SystemExit(
            "memory_embedding_index_failed: check approved settings, model, schema and grants"
        ) from None
    print(result)


if __name__ == "__main__":
    main()
