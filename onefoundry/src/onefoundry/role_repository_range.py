"""Character-offset excerpts of a verified, exact repository document."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from onefoundry import role_repository_file as file
from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from onefoundry.role_tool_admission import RunContext
    from onefoundry.settings import Settings

RANGE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        **file.REPOSITORY_FILE_PARAMETERS["properties"],
        "offset": {"type": "integer", "minimum": 0},
    },
    "required": ["source_id", "path", "offset"],
    "additionalProperties": False,
}


class RangeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    path: str
    offset: int = Field(ge=0)


def parse_repository_range(arguments: object) -> dict[str, Any]:
    parsed = RangeArguments.model_validate(arguments)
    file.parse_repository_file({"source_id": parsed.source_id, "path": parsed.path})
    return parsed.model_dump()


def repository_range(
    connection: Connection, context: RunContext, arguments: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    document = file.repository_document(connection, context, arguments, settings)
    if not document["found"]:
        return document
    content = document.pop("text")
    assert isinstance(content, str)
    offset = arguments["offset"]
    if offset > len(content):
        raise ValueError("repository offset outside document")
    byte_limit = settings.live_text_max_output_bytes // 2
    page = content[offset : offset + byte_limit].encode("utf-8")[:byte_limit]
    page_text = page.decode("utf-8", errors="ignore")
    next_offset = offset + len(page_text)
    document.pop("truncated")
    document.update(
        offset=offset,
        text=page_text,
        next_offset=next_offset if next_offset < len(content) else None,
    )
    validate_repository_range(document)
    return document


def validate_repository_range(result: dict[str, Any]) -> None:
    if result.get("found") is False:
        file.validate_repository_file(result)
        return
    if set(result) != {
        "source_id",
        "found",
        "revision",
        "path",
        "sha256",
        "byte_size",
        "offset",
        "text",
        "next_offset",
    }:
        raise ValueError("repository range result fields invalid")
    file._Arguments.canonical_uuid(result["source_id"])
    offset, next_offset, content = result["offset"], result["next_offset"], result["text"]
    if (
        result["found"] is not True
        or not isinstance(result["revision"], str)
        or file.SHA40.fullmatch(result["revision"]) is None
        or not isinstance(result["path"], str)
        or not file._valid_path(result["path"])
        or not isinstance(result["sha256"], str)
        or file.SHA256.fullmatch(result["sha256"]) is None
        or type(result["byte_size"]) is not int
        or result["byte_size"] < 0
        or type(offset) is not int
        or not 0 <= offset <= result["byte_size"]
        or not isinstance(content, str)
        or contains_sensitive_text(content)
        or (
            next_offset is not None
            and (
                type(next_offset) is not int
                or next_offset != offset + len(content)
                or next_offset <= offset
                or next_offset > result["byte_size"]
            )
        )
    ):
        raise ValueError("repository range result invalid")
