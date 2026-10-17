"""Approval-bound document writes into a watched folder.

A role with ``propose_document_write_v1`` proposes one new text, Markdown, or CSV
file for a watched folder whose account has write access. The approval request
shows the exact content and binds its SHA-256. After approval, the operator
explicitly dispatches it once; the file is created (never overwritten), read back,
and verified. Uncertain outcomes become unknown and are never retried.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from onefoundry.document_api_sources import DocumentError
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import (
    CALL_ID,
    ToolDenied,
    ToolInputError,
    ToolOutcomeUnknown,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
)
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.document_store import DocumentStore
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "propose_document_write_v1"
EXTENSIONS = {"text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv"}
COLUMNS = (
    "id,source_id,work_order_id,run_id,approval_id,folder_id,file_name,mime_type,content_sha256,state,error_code,"
    "provider_file_id,readback,created_at,finished_at"
)
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "format": "uuid"},
        "file_name": {"type": "string", "maxLength": 200},
        "mime_type": {"type": "string", "enum": list(EXTENSIONS)},
        "content": {"type": "string", "maxLength": 65536},
    },
    "required": ["source_id", "file_name", "mime_type", "content"],
    "additionalProperties": False,
}


def parse_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"source_id", "file_name", "mime_type", "content"}:
        raise ToolInputError("document write arguments are invalid")
    try:
        source_id = UUID(str(value["source_id"]))
    except ValueError:
        raise ToolInputError("document write source is invalid") from None
    name, mime, content = value["file_name"], value["mime_type"], value["content"]
    if mime not in EXTENSIONS or not isinstance(name, str) or not isinstance(content, str):
        raise ToolInputError("document write arguments are invalid")
    name = name.strip()
    if (
        not 1 <= len(name) <= 200
        or any(char in name for char in '/\\:*?"<>|')
        or any(ord(char) < 32 for char in name)
        or not name.lower().endswith(EXTENSIONS[mime])
    ):
        raise ToolInputError(f"file name must be plain and end with {EXTENSIONS[mime]}")
    if not content.strip() or len(content.encode("utf-8")) > 65536:
        raise ToolInputError("document content must be 1-65536 bytes")
    try:
        local_text(content)
    except ValueError:
        raise ToolInputError("document content appears to contain a secret") from None
    if contains_sensitive_text(content):
        raise ToolInputError("document content appears to contain a secret")
    return {"source_id": str(source_id), "file_name": name, "mime_type": mime, "content": content}


def _digest(source_id: str, folder_id: str, name: str, mime: str, content_sha: str) -> str:
    canonical = json.dumps(
        {
            "source_id": source_id,
            "folder_id": folder_id,
            "file_name": name,
            "mime_type": mime,
            "content_sha256": content_sha,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("document write tool version is not admitted")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    deadline = monotonic() + settings.live_text_timeout_seconds
    try:
        with gateway.engine.begin() as connection:
            _db_bounds(connection, deadline)
            context = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            prior = _prior_attempt(connection, project_id, run_id, call_id)
            if prior == "role_tool_started":
                raise ToolOutcomeUnknown(
                    "document write proposal outcome is unknown; inspect the run timeline"
                )
            if prior is not None:
                raise ToolDenied("document write call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("document write tool is not admitted for this run")
            parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
            source = (
                connection.execute(
                    text(
                        "SELECT d.label,d.folder_id,d.status,c.features,c.status AS account_status "
                        "FROM document_sources d JOIN connector_accounts c ON c.project_id=d.project_id "
                        "AND c.id=d.connector_account_id WHERE d.project_id=:p AND d.id=:s"
                    ),
                    {"p": project_id, "s": UUID(parsed["source_id"])},
                )
                .mappings()
                .one_or_none()
            )
            if source is None or source["status"] != "ready" or not source["folder_id"]:
                raise ToolDenied("the watched folder is not ready")
            if "files_write" not in list(source["features"]):
                raise ToolDenied("the folder's account was not connected with write access")
            content_sha = hashlib.sha256(parsed["content"].encode("utf-8")).hexdigest()
            digest = _digest(
                parsed["source_id"],
                source["folder_id"],
                parsed["file_name"],
                parsed["mime_type"],
                content_sha,
            )
            _event(
                gateway,
                connection,
                context,
                "role_tool_started",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "version": 1,
                    "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                    "argument_bytes": len(encoded),
                },
            )
            approval_id, write_id = uuid4(), uuid4()
            shown = (
                parsed["content"]
                if len(parsed["content"]) <= 14000
                else parsed["content"][:14000]
                + "\n[…truncated in this preview; the exact content is bound by its SHA-256]"
            )
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id,project_id,actor_id,work_order_id,run_id,action_type,target,"
                    "content,proposal_digest,status) VALUES "
                    "(:id,:p,:actor,:o,:r,'publish',:target,:content,:digest,"
                    "'pending')"
                ),
                {
                    "id": approval_id,
                    "p": project_id,
                    "actor": f"profile:{profile_id}",
                    "o": context.work_order_id,
                    "r": run_id,
                    "target": f"{source['label']}/{parsed['file_name']}"[:500],
                    "content": (
                        f"Create new file “{parsed['file_name']}” ({parsed['mime_type']}) in watched folder "
                        f"“{source['label']}”. Content SHA-256 {content_sha}.\n\n{shown}"
                    )[:16000],
                    "digest": digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO document_writes "
                    "(id,project_id,actor_id,source_id,work_order_id,run_id,approval_id,"
                    "folder_id,file_name,mime_type,content,content_sha256) VALUES "
                    "(:id,:p,:actor,:s,:o,:r,:a,:f,"
                    ":name,:mime,:content,:sha)"
                ),
                {
                    "id": write_id,
                    "p": project_id,
                    "actor": f"profile:{profile_id}",
                    "s": UUID(parsed["source_id"]),
                    "o": context.work_order_id,
                    "r": run_id,
                    "a": approval_id,
                    "f": source["folder_id"],
                    "name": parsed["file_name"],
                    "mime": parsed["mime_type"],
                    "content": parsed["content"],
                    "sha": content_sha,
                },
            )
            result = {"approval_id": str(approval_id), "write_id": str(write_id), "status": "pending"}
            result_bytes = json.dumps(result, separators=(",", ":")).encode()
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                    "result_bytes": len(result_bytes),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": result,
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        }
    except DBAPIError:
        raise ToolOutcomeUnknown(
            "document write transaction outcome is unknown; inspect the run timeline"
        ) from None


def apply_decision(
    gateway: ToolGateway, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str
) -> None:
    """Inside the approval transaction: record the decision on a bound document write."""
    row = (
        connection.execute(
            text(
                "SELECT id,source_id,folder_id,file_name,mime_type,content_sha256 FROM document_writes "
                "WHERE project_id=:p AND approval_id=:a AND state='pending' FOR UPDATE"
            ),
            {"p": project_id, "a": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    expected = _digest(
        str(row["source_id"]), row["folder_id"], row["file_name"], row["mime_type"], row["content_sha256"]
    )
    if expected != approval["proposal_digest"]:
        raise Conflict("document write no longer matches its approval request")
    state = "approved" if decision == "approved" else "rejected"
    connection.execute(
        text("UPDATE document_writes SET state=:s WHERE project_id=:p AND id=:id"),
        {"s": state, "p": project_id, "id": row["id"]},
    )
    gateway._event(
        connection, project_id, None, None, f"document_write_{state}", {"write_id": str(row["id"])}
    )


def list_writes(gateway: ToolGateway, project_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT {COLUMNS} FROM document_writes WHERE project_id=:p ORDER BY created_at DESC,id"),  # noqa: S608
            {"p": project_id},
        ).mappings()
        return [dict(row) for row in rows]


def _finish(
    gateway: ToolGateway, project_id: UUID, write_id: UUID, state: str, error: str | None, **extra: Any
) -> None:
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE document_writes SET "
                "state=:s,error_code=:e,provider_file_id=COALESCE(:f,provider_file_id),"
                "readback=:rb,finished_at=clock_timestamp() WHERE project_id=:p AND id=:id AND "
                "state='dispatching'"
            ).bindparams(bindparam("rb", type_=JSONB(none_as_null=True))),
            {
                "s": state,
                "e": error,
                "f": extra.get("file_id"),
                "rb": extra.get("readback"),
                "p": project_id,
                "id": write_id,
            },
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            f"document_write_{state}",
            {"write_id": str(write_id), "error_code": error, "provider_file_id": extra.get("file_id")},
        )


def dispatch(
    gateway: ToolGateway, documents: DocumentStore, project_id: UUID, write_id: UUID
) -> dict[str, Any]:
    """Write the approved file exactly once, then read it back and verify it."""
    with gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT w.*,a.status AS approval_status FROM document_writes w JOIN approval_requests a "
                    "ON a.project_id=w.project_id AND a.id=w.approval_id WHERE w.project_id=:p AND w.id=:id "
                    "FOR UPDATE OF w"
                ),
                {"p": project_id, "id": write_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("document write not found")
        if row["state"] != "approved" or row["approval_status"] != "approved":
            raise Conflict("only an approved, not yet dispatched write can be sent")
        if hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest() != row["content_sha256"]:
            raise Conflict("document write content no longer matches its approved digest")
        connection.execute(
            text("UPDATE document_writes SET state='dispatching' WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": write_id},
        )
        gateway._event(
            connection, project_id, None, None, "document_write_dispatching", {"write_id": str(write_id)}
        )
        source = documents._row(connection, project_id, row["source_id"])
    data = str(row["content"]).encode("utf-8")
    try:
        client = documents.client(project_id, source)
        written = client.upload(row["folder_id"], row["file_name"], data, row["mime_type"])
    except DocumentError as failure:
        state = "failed" if failure.code == "write_failed" else "unknown"
        _finish(gateway, project_id, write_id, state, failure.code)
        return next(item for item in list_writes(gateway, project_id) if item["id"] == write_id)
    except Unavailable:
        _finish(gateway, project_id, write_id, "failed", "credential_unavailable")
        return next(item for item in list_writes(gateway, project_id) if item["id"] == write_id)
    try:
        observed = client.read_back(written.file_id)
    except (DocumentError, Unavailable):
        _finish(gateway, project_id, write_id, "unknown", "readback_failed", file_id=written.file_id)
        return next(item for item in list_writes(gateway, project_id) if item["id"] == write_id)
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    verified = observed.size == len(data) and (source["provider"] != "gdrive" or observed.revision == md5)
    readback = {
        "name": observed.name,
        "size": observed.size,
        "revision": observed.revision[:200],
        "verified": verified,
    }
    _finish(
        gateway,
        project_id,
        write_id,
        "done" if verified else "unknown",
        None if verified else "readback_mismatch",
        file_id=written.file_id,
        readback=readback,
    )
    return next(item for item in list_writes(gateway, project_id) if item["id"] == write_id)
