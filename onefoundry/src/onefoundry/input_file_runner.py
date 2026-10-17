"""Bind extracted project and work-order text into a coding-runner manifest."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.input_file_context import ContextSelection, select_context
from onefoundry.settings import Settings


def prepare_runner_context(
    connection: Any,
    settings: Settings,
    project_id: UUID,
    order_id: UUID,
    instructions: str,
) -> tuple[str, ContextSelection]:
    """Prepare exactly the text a bounded 4096-byte runner instruction can carry."""
    budget = 4096 - len(instructions.encode("utf-8")) - 2
    selected = select_context(
        connection,
        project_id,
        order_id,
        [],
        instructions,
        max(0, budget),
        settings.input_file_max_link_count,
    )
    result = instructions + ("\n\n" + selected.text if selected.text else "")
    if len(result.encode("utf-8")) > 4096:
        raise Conflict("attached text exceeds the coding-runner instruction limit")
    return result, selected


def record_runner_context(
    connection: Any,
    project_id: UUID,
    run_id: UUID,
    actor_id: str,
    selection: ContextSelection,
) -> None:
    for use in selection.uses:
        connection.execute(
            text(
                "INSERT INTO input_file_context_uses "
                "(id,project_id,actor_id,run_id,file_id,chunk_id,kind,source_label,sha256,byte_count) "
                "VALUES (:id,:project_id,:actor_id,:run_id,:file_id,:chunk_id,'text',:label,:sha,:size)"
            ),
            {
                "id": uuid4(),
                "project_id": project_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "file_id": use.file_id,
                "chunk_id": use.chunk_id,
                "label": use.source_label,
                "sha": use.chunk_sha256,
                "size": use.byte_count,
            },
        )
