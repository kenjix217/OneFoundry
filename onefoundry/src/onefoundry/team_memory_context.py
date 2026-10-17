"""Approved fact selection for a bounded, reproducible team turn snapshot."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection

from onefoundry.memory_policy import work_memory_query
from onefoundry.memory_store import retrieve_memory
from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from onefoundry.memory_embedding_settings import EmbeddingConfiguration


def memory_context(
    connection: Connection,
    project_id: UUID,
    row: Any,
    parts: list[str],
    max_bytes: int | None,
    max_items: int,
    settings: "EmbeddingConfiguration | None" = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if row["prompt_sha256"] is not None:
        # Once prepared, re-use the exact fact snapshot instead of silently changing
        # an already recorded provider input. A new attempt retrieves current facts.
        memories = row["context_memory"]
    else:
        scope = "project" if row["memory_scope"] == "project" else None
        memories = retrieve_memory(
            connection,
            project_id,
            work_memory_query(str(row["title"]), str(row["work_instructions"])),
            max_items,
            scope,
            settings,
        )
    selected: list[dict[str, Any]] = []
    for memory in memories:
        if contains_sensitive_text(str(memory["text"])):
            continue
        fact = {
            "id": str(memory["id"]),
            "project_id": str(memory["project_id"]),
            "scope": memory["scope"],
            "text": memory["text"],
            "source_result_id": str(memory["source_result_id"]),
            "source_candidate_id": str(memory["source_candidate_id"]),
            "created_at": str(memory["created_at"]),
        }
        citation = (
            f"Approved memory at turn preparation [{fact['id']}; {fact['scope']}; "
            f"result {fact['source_result_id']}; candidate {fact['source_candidate_id']}]: {fact['text']}"
        )
        if max_bytes is not None and len("\n\n".join([*parts, citation]).encode()) > max_bytes:
            continue
        parts.append(citation)
        selected.append(fact)
    return parts, selected
