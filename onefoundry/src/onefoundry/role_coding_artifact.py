"""Bounded saved diff/log text for an admitted role tool.

The read is anchored to a terminal external-code attempt and its matching
append-only receipt event. Paths and provider details stay inside the gateway.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import Connection, text

from onefoundry.role_coding_evidence import _receipt_tests
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.role_tool_admission import RunContext

CODING_ARTIFACT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string", "format": "uuid"},
        "artifact_id": {"type": "string", "format": "uuid"},
    },
    "required": ["run_id", "artifact_id"],
    "additionalProperties": False,
}
MAX_TEXT_BYTES = 8192


class CodingArtifactArguments(StrictModel):
    run_id: UUID
    artifact_id: UUID


class CodingArtifactResult(StrictModel):
    run_id: UUID
    artifact_id: UUID
    found: bool
    kind: Literal["diff", "log"] | None
    sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int | None = Field(ge=0, le=65536)
    text: str | None
    truncated: bool

    @model_validator(mode="after")
    def proved_content(self) -> CodingArtifactResult:
        if not self.found and (
            self.kind is not None
            or self.sha256 is not None
            or self.byte_size is not None
            or self.text is not None
            or self.truncated
        ):
            raise ValueError("absent artifact cannot carry content")
        if self.found and (
            self.kind is None or self.sha256 is None or self.byte_size is None or self.text is None
        ):
            raise ValueError("found artifact requires verified content")
        if self.text is not None and len(self.text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("artifact text exceeds limit")
        return self


def parse_coding_artifact(arguments: object) -> dict[str, Any]:
    return CodingArtifactArguments.model_validate(arguments).model_dump(mode="json")


def validate_coding_artifact(result: dict[str, Any]) -> None:
    CodingArtifactResult.model_validate(result)


def _safe_text(data: bytes) -> tuple[str, bool]:
    # Imported lazily: role_tool_admission registers this module at import time.
    from onefoundry.role_tool_admission import ToolOutputRejected

    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolOutputRejected("coding artifact is not UTF-8 text") from None
    if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in decoded):
        raise ToolOutputRejected("coding artifact contains binary control bytes")
    if contains_sensitive_text(decoded):
        raise ToolOutputRejected("coding artifact appears to contain a secret")
    excerpt = data[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return excerpt, len(data) > MAX_TEXT_BYTES


def coding_artifact(
    connection: Connection,
    context: RunContext,
    arguments: dict[str, Any],
    spool: RunnerSpool,
) -> dict[str, Any]:
    from onefoundry.role_tool_admission import ToolOutputRejected

    run_id = UUID(arguments["run_id"])
    artifact_id = UUID(arguments["artifact_id"])
    absent: dict[str, Any] = {
        "run_id": str(run_id),
        "artifact_id": str(artifact_id),
        "found": False,
        "kind": None,
        "sha256": None,
        "byte_size": None,
        "text": None,
        "truncated": False,
    }
    row = (
        connection.execute(
            text(
                "SELECT r.status AS run_status,a.id AS attempt_id,a.adapter_id,a.status,"
                "a.reason_code,a.diff_sha256,a.receipt_sha256,a.tests,"
                "f.kind,f.path,f.sha256,f.byte_size "
                "FROM external_code_artifacts f "
                "JOIN external_code_attempts a ON a.project_id=f.project_id "
                "AND a.run_id=f.run_id AND a.id=f.attempt_id "
                "JOIN runs r ON r.project_id=f.project_id AND r.id=f.run_id "
                "WHERE f.project_id=:project_id AND f.run_id=:run_id AND f.id=:artifact_id "
                "AND r.mode='external_code' LIMIT 1"
            ),
            {
                "project_id": str(context.project_id),
                "run_id": str(run_id),
                "artifact_id": str(artifact_id),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["status"] not in ("done", "failed") or row["run_status"] != row["status"]:
        return absent
    attempt = dict(row)
    attempt["id"] = attempt["attempt_id"]
    if isinstance(attempt["tests"], str):
        try:
            attempt["tests"] = json.loads(attempt["tests"])
        except ValueError:
            return absent
    provenance, _tests = _receipt_tests(connection, context.project_id, run_id, attempt)
    if provenance != "reconciled_runner_receipt":
        return absent
    if row["kind"] not in ("diff", "log") or (row["kind"] == "diff" and row["sha256"] != row["diff_sha256"]):
        return absent
    try:
        data = spool.read_artifact(row["attempt_id"], row["path"], row["byte_size"], row["sha256"])
    except (OSError, SpoolInvalid):
        raise ToolOutputRejected("coding artifact failed saved byte verification") from None
    excerpt, truncated = _safe_text(data)
    return {
        "run_id": str(run_id),
        "artifact_id": str(artifact_id),
        "found": True,
        "kind": row["kind"],
        "sha256": row["sha256"],
        "byte_size": row["byte_size"],
        "text": excerpt,
        "truncated": truncated,
    }
