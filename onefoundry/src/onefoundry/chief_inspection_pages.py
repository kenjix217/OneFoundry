"""Bounded, secret-filtered pages for saved Chief oversight records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_dependency_context import TeamContextBudgetExceeded
from onefoundry.workflow_schemas import StrictModel


class InspectionPageArgs(StrictModel):
    offset: int = Field(default=0, ge=0)
    content_offset: int = Field(default=0, ge=0)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_before: datetime | None = None

    @field_validator("created_before")
    @classmethod
    def aware_cutoff(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("inspection cutoff requires a timezone")
        return value


def anchor_page[PageArguments: InspectionPageArgs](
    connection: Connection, arguments: PageArguments
) -> PageArguments:
    now = connection.scalar(text("SELECT clock_timestamp()"))
    if arguments.created_before is not None and arguments.created_before > now:
        raise ValueError("inspection cutoff cannot be in the future")
    if arguments.content_offset and arguments.created_before is None:
        raise ValueError("continued content reads require the original inspection cutoff")
    return arguments.model_copy(update={"created_before": arguments.created_before or now})


class InspectionStateChanged(Conflict):
    code = "inspection_state_changed"

    @staticmethod
    def correction() -> dict[str, Any]:
        return {
            "reason": InspectionStateChanged.code,
            "next_step": "Restart this record page with content_offset=0 and no expected_sha256. "
            "Saved status changed during the read; preserve the previously observed evidence.",
        }


def _public(value: Any) -> Any:
    if isinstance(value, str):
        return "[held: sensitive content]" if contains_sensitive_text(value) else value
    if isinstance(value, dict):
        return {_public(key): _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def inspection_page(
    document: dict[str, Any],
    arguments: InspectionPageArgs,
    byte_budget: int,
    next_offset: int | None,
) -> dict[str, Any]:
    body = json.dumps(_public(document), ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    if arguments.content_offset and arguments.expected_sha256 is None:
        raise ValueError("continued content reads require the previous page digest")
    if arguments.expected_sha256 is not None and arguments.expected_sha256 != digest:
        raise InspectionStateChanged("saved status changed; restart this record page with a fresh read")
    if arguments.content_offset > len(body):
        raise ValueError("content offset exceeds the saved record page")
    result: dict[str, Any] = {
        "offset": arguments.offset,
        "content_offset": arguments.content_offset,
        "body_sha256": digest,
        "total_characters": len(body),
        "content": "",
        "next_content_offset": arguments.content_offset,
        "next_offset": None,
        "created_before": arguments.created_before.isoformat() if arguments.created_before else None,
    }
    low, high = arguments.content_offset, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {
            **result,
            "content": body[arguments.content_offset : middle],
            "next_content_offset": None if middle == len(body) else middle,
            "next_offset": next_offset if middle == len(body) else None,
        }
        if len(json.dumps(candidate).encode()) <= byte_budget:
            low = middle
        else:
            high = middle - 1
    result.update(
        content=body[arguments.content_offset : low],
        next_content_offset=None if low == len(body) else low,
        next_offset=next_offset if low == len(body) else None,
    )
    if len(json.dumps(result).encode()) > byte_budget or (
        low == arguments.content_offset and low != len(body)
    ):
        raise TeamContextBudgetExceeded("inspection page metadata exceeds the context budget")
    return result


def record_page(
    connection: Connection,
    query: str,
    parameters: dict[str, Any],
    arguments: InspectionPageArgs,
    max_items: int,
) -> tuple[list[dict[str, Any]], int | None]:
    rows = (
        connection.execute(
            text(query + " LIMIT :page_limit OFFSET :page_offset"),
            {
                **parameters,
                "page_limit": max_items + 1,
                "page_offset": arguments.offset,
                "created_before": arguments.created_before,
            },
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows[:max_items]], (
        arguments.offset + max_items if len(rows) > max_items else None
    )
