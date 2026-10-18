"""Exact, scoped pages of operator-attached text for an active team turn."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_dependency_context import TeamContextBudgetExceeded
from onefoundry.workflow_schemas import StrictModel


class InputFilePageArgs(StrictModel):
    file_id: UUID | None = None
    offset: int = Field(default=0, ge=0)


def safe_file_label(display_name: str) -> str:
    return "[sensitive filename]" if contains_sensitive_text(display_name) else display_name


def scoped_text_files(connection: Any, project_id: UUID, root_work_order_id: UUID) -> list[dict[str, Any]]:
    """Only project links and links to this root order are available to its team."""
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT f.id,f.display_name,f.extension,f.sha256,f.byte_size,f.status "
                "FROM input_files f WHERE f.project_id=:project AND EXISTS ("
                "SELECT 1 FROM input_file_links l WHERE l.project_id=f.project_id AND l.file_id=f.id "
                "AND (l.scope='project' OR (l.scope='work_order' AND l.work_order_id=:root))) "
                "ORDER BY f.created_at,f.id"
            ),
            {"project": project_id, "root": root_work_order_id},
        )
        .mappings()
        .all()
    ]


def _extracted_text(connection: Any, project_id: UUID, file_id: UUID) -> str:
    rows = (
        connection.execute(
            text(
                "SELECT ordinal,source_label,content,sha256 FROM input_file_chunks "
                "WHERE project_id=:project AND file_id=:file ORDER BY ordinal"
            ),
            {"project": project_id, "file": file_id},
        )
        .mappings()
        .all()
    )
    if not rows or any(row["ordinal"] != ordinal for ordinal, row in enumerate(rows)):
        raise Conflict("input file extracted text is incomplete")
    sections: list[str] = []
    for row in rows:
        content = row["content"]
        if hashlib.sha256(content.encode()).hexdigest() != row["sha256"] or contains_sensitive_text(content):
            raise Conflict("input file extracted text failed integrity or safety checks")
        label = row["source_label"]
        if contains_sensitive_text(label):
            label = str(row["ordinal"] + 1)
        sections.append(f"Section: {label}\n{content}")
    return "\n\n".join(sections)


def read_input_file_page(
    connection: Any,
    root: str,
    project_id: UUID,
    root_work_order_id: UUID,
    arguments: InputFilePageArgs,
    byte_budget: int,
) -> dict[str, Any]:
    if arguments.file_id is None:
        return _file_index(connection, project_id, root_work_order_id, arguments.offset, byte_budget)
    row = next(
        (
            item
            for item in scoped_text_files(connection, project_id, root_work_order_id)
            if item["id"] == arguments.file_id
        ),
        None,
    )
    if row is None or row["status"] != "ready_text":
        raise NotFound("input file is not ready in this work order")
    try:
        original = InputFileBytes(root).read(project_id, arguments.file_id, row["sha256"], row["byte_size"])
    except InputFileInvalid:
        raise Conflict("input file failed its storage integrity check") from None
    if row["extension"] == "txt":
        try:
            body = original.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise Conflict("input file is not valid UTF-8") from None
        representation = "original_utf8"
    else:
        body = _extracted_text(connection, project_id, arguments.file_id)
        representation = "extracted_sections"
    if contains_sensitive_text(body):
        raise Conflict("input file contains sensitive text")
    if arguments.offset > len(body):
        raise ValueError("input file offset exceeds available content")
    result: dict[str, Any] = {
        "file_id": str(arguments.file_id),
        "display_name": safe_file_label(row["display_name"]),
        "file_sha256": row["sha256"],
        "representation": representation,
        "representation_sha256": hashlib.sha256(body.encode()).hexdigest(),
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
        raise TeamContextBudgetExceeded("input file page metadata exceeds the context budget")
    return result


def _file_index(
    connection: Any, project_id: UUID, root_work_order_id: UUID, offset: int, byte_budget: int
) -> dict[str, Any]:
    files = [
        {
            "file_id": str(row["id"]),
            "display_name": safe_file_label(row["display_name"]),
            "file_sha256": row["sha256"],
        }
        for row in scoped_text_files(connection, project_id, root_work_order_id)
        if row["status"] == "ready_text"
    ]
    if offset > len(files):
        raise ValueError("input file index offset exceeds available files")
    result: dict[str, Any] = {"files": [], "offset": offset, "total_files": len(files), "next_offset": None}
    end = offset
    while end < len(files):
        candidate = {**result, "files": files[offset : end + 1], "next_offset": end + 1}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > byte_budget:
            break
        end += 1
    result["files"] = files[offset:end]
    result["next_offset"] = None if end == len(files) else end
    if (
        len(json.dumps(result, ensure_ascii=False).encode()) > byte_budget
        or end == offset
        and end != len(files)
    ):
        raise TeamContextBudgetExceeded("input file index exceeds the context budget")
    return result
