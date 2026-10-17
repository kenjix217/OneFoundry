"""Bounded projections and exact paging of confirmed, declared team inputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_dependency_availability import availability, qa_feedback, scoped_assignment
from onefoundry.workflow_schemas import StrictModel


class TeamContextBudgetExceeded(Conflict):
    """Required team input references cannot fit the configured prompt budget."""


class DependencyPageArgs(StrictModel):
    assignment_id: UUID
    offset: int = Field(default=0, ge=0)


def dependency_results(connection: Any, project_id: UUID, row: Any, assignment: Any) -> list[dict[str, Any]]:
    if assignment is not None:
        query = (
            "SELECT previous.id AS assignment_id,previous.acceptance_kind,m.id AS message_id,m.body "
            "FROM task_session_dependencies d JOIN task_session_assignments previous "
            "ON previous.project_id=d.project_id AND previous.id=d.depends_on_id "
            "JOIN task_session_messages m ON m.project_id=previous.project_id "
            "AND m.id=previous.result_message_id "
            "WHERE d.project_id=:p AND d.assignment_id=:assignment "
            "AND previous.status='satisfied' ORDER BY previous.created_at,previous.id"
        )
        parameters = {"p": project_id, "assignment": assignment["id"]}
    else:
        query = (
            "SELECT child.id AS assignment_id,child.acceptance_kind,m.id AS message_id,m.body "
            "FROM task_session_assignments child JOIN task_session_messages m "
            "ON m.project_id=child.project_id AND m.id=child.result_message_id "
            "WHERE child.project_id=:p AND child.parent_session_id=:s "
            "AND child.status='satisfied' ORDER BY child.created_at,child.id"
        )
        parameters = {"p": project_id, "s": row["session_id"]}
    return [dict(item) for item in connection.execute(text(query), parameters).mappings().all()]


def _reference(item: dict[str, Any]) -> dict[str, Any]:
    body = str(item["body"])
    if contains_sensitive_text(body):
        raise Conflict("dependency result contains sensitive text")
    reference: dict[str, Any] = {
        "assignment_id": str(item["assignment_id"]),
        "message_id": str(item["message_id"]),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "body_bytes": len(body.encode()),
        "context_complete": False,
        "next_step": "Read every page with onefoundry.read_dependency_result before evaluating this input.",
    }
    summary = body
    try:
        saved = json.loads(body)
    except (ValueError, TypeError):
        saved = None
    if (
        item.get("acceptance_kind") == "artifact"
        and isinstance(saved, dict)
        and isinstance(saved.get("artifact"), dict)
    ):
        artifact = saved["artifact"]
        reference["artifact"] = {
            key: artifact[key]
            for key in ("artifact_id", "run_id", "sha256", "kind", "playback_verified")
            if key in artifact
        }
        summary = str(saved.get("summary", ""))
    reference["summary_excerpt"] = summary
    reference["summary_truncated"] = False
    return reference


def project_dependencies(items: list[dict[str, Any]], byte_budget: int | None) -> list[str]:
    """Keep small inputs intact; advertise omitted bytes and an exact scoped read."""
    references = [_reference(item) for item in items]
    full = [
        f"Confirmed result from assignment {item['assignment_id']} "
        f"(message {item['message_id']}): {item['body']}"
        for item in items
    ]
    if byte_budget is None or len("\n\n".join(full).encode()) <= byte_budget:
        return full
    if not items:
        return []
    slot = (byte_budget - len(b"\n\n") * (len(items) - 1)) // len(items)
    projected: list[str] = []
    for reference in references:
        summary = str(reference.pop("summary_excerpt"))
        low, high = 0, len(summary)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {
                **reference,
                "summary_excerpt": summary[:middle],
                "summary_truncated": middle < len(summary),
            }
            if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()) <= slot:
                low = middle
            else:
                high = middle - 1
        reference.update(summary_excerpt=summary[:low], summary_truncated=low < len(summary))
        serialized = json.dumps(reference, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode()) > slot:
            raise TeamContextBudgetExceeded("confirmed dependency references exceed the context budget")
        projected.append(serialized)
    return projected


def read_dependency_page(
    connection: Any,
    project_id: UUID,
    session_id: UUID,
    arguments: DependencyPageArgs,
    byte_budget: int,
) -> dict[str, Any]:
    assignment = (
        connection.execute(
            text("SELECT id FROM task_session_assignments WHERE project_id=:p AND child_session_id=:s"),
            {"p": project_id, "s": session_id},
        )
        .mappings()
        .one_or_none()
    )
    rows = dependency_results(connection, project_id, {"session_id": session_id}, assignment)
    item = next((row for row in rows if row["assignment_id"] == arguments.assignment_id), None)
    if item is None:
        target = scoped_assignment(connection, project_id, session_id, arguments.assignment_id, assignment)
        state = availability(target)
        feedback = qa_feedback(connection, project_id, target)
        if feedback is not None:
            return _content_page(
                feedback,
                arguments,
                byte_budget,
                {**state, "content_available": True, "result_kind": "review_feedback"},
            )
        if len(json.dumps(state, ensure_ascii=False).encode()) > byte_budget:
            raise TeamContextBudgetExceeded("assignment availability exceeds the context budget")
        return state
    return _content_page(item, arguments, byte_budget)


def _content_page(
    item: dict[str, Any],
    arguments: DependencyPageArgs,
    byte_budget: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reference = _reference(item)
    body = str(item["body"])
    if arguments.offset > len(body):
        raise ValueError("result offset exceeds the saved content")
    result = {
        **(metadata or {}),
        "assignment_id": reference["assignment_id"],
        "message_id": reference["message_id"],
        "body_sha256": reference["body_sha256"],
        "offset": arguments.offset,
        "total_characters": len(body),
        "content": "",
        "next_offset": arguments.offset,
    }
    low, high = arguments.offset, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {**result, "content": body[arguments.offset : middle], "next_offset": middle}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) <= byte_budget:
            low = middle
        else:
            high = middle - 1
    result.update(content=body[arguments.offset : low], next_offset=None if low == len(body) else low)
    if len(json.dumps(result, ensure_ascii=False).encode()) > byte_budget or (
        low == arguments.offset and low != len(body)
    ):
        raise TeamContextBudgetExceeded("result page metadata exceeds the context budget")
    return result
