"""Folder reads, proposal snapshots, and verified one-shot document actions."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from onefoundry import document_action_store
from onefoundry.document_access import DocumentAccess
from onefoundry.document_action_schemas import (
    ACTION,
    EditCells,
    RangeRead,
    ReplaceFile,
    Search,
    ShareFile,
    dimensions,
    parse_action,
)
from onefoundry.document_api_sources import GOOGLE_EXPORTS, Change, DocumentError
from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.input_file_bytes import InputFileInvalid

if TYPE_CHECKING:
    from onefoundry.document_store import DocumentStore


def access(
    store: DocumentStore, project_id: UUID, source_id: UUID, *, write: bool = False
) -> tuple[dict[str, Any], DocumentAccess]:
    with store.gateway.engine.connect() as connection:
        source = store._row(connection, project_id, source_id)
        account = store.connectors._row(connection, project_id, source["connector_account_id"])
    if source["status"] != "ready" or not source["folder_id"] or account["status"] != "connected":
        raise Conflict("the selected folder or account is not ready")
    if write and "files_write" not in account["features"]:
        raise Conflict("connect this folder's account with write access first")
    if write:
        from sqlalchemy import text

        with store.gateway.engine.connect() as connection:
            if (
                connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
                != "active"
            ):
                raise Conflict("only an active project may change external files")
    source["account_version"] = account["version"]
    return source, DocumentAccess(store.client(project_id, source), source["folder_id"])


def search(store: DocumentStore, project_id: UUID, data: Search) -> dict[str, Any]:
    _, client = access(store, project_id, data.source_id)
    return client.search(
        data.query,
        max_pages=store.settings.document_search_max_pages,
        limit=store.settings.document_search_max_results,
    )


def read_range(store: DocumentStore, project_id: UUID, data: RangeRead) -> dict[str, Any]:
    _, client = access(store, project_id, data.source_id)
    return {
        "sheet": data.sheet,
        "address": data.address,
        "values": client.range(data, store.settings.document_max_range_cells),
    }


def _input(store: DocumentStore, project_id: UUID, work_order_id: UUID, input_id: UUID) -> dict[str, Any]:
    files = store.gateway.input_file_store
    if files is None:
        raise Unavailable("input-file storage is unavailable")
    row = next((row for row in files.list(project_id, work_order_id) if row["id"] == input_id), None)
    if row is None or row["status"] == "rejected":
        raise Conflict("the replacement must be a readable file attached to this project or work order")
    if row["byte_size"] > store.settings.document_max_bytes:
        raise Conflict("replacement exceeds the document byte limit")
    return row


def prepare(store: DocumentStore, project_id: UUID, work_order_id: UUID, raw: object) -> dict[str, Any]:
    data = parse_action(raw)
    source, client = access(store, project_id, data.source_id, write=True)
    snapshot = client.metadata(data.file_id)
    proposal: dict[str, Any] = {
        "action": data.model_dump(mode="json"),
        "snapshot": snapshot,
        "source_version": source["version"],
        "account_version": source["account_version"],
        "folder_label": source["label"],
    }
    if isinstance(data, ReplaceFile):
        if snapshot["mime_type"] in GOOGLE_EXPORTS:
            raise Conflict(
                "native Google documents need a document-specific edit; use cell editing for Sheets"
            )
        row = _input(store, project_id, work_order_id, data.input_file_id)
        if row["mime_type"] != snapshot["mime_type"]:
            raise Conflict("replacement file type must match the existing file")
        proposal["input"] = {
            "sha256": row["sha256"],
            "byte_size": row["byte_size"],
            "name": row["display_name"],
        }
    elif isinstance(data, EditCells):
        rows, columns = dimensions(data.address)
        if (
            rows * columns > store.settings.document_max_range_cells
            or len(data.values) != rows
            or any(len(row) != columns for row in data.values)
        ):
            raise Conflict("cell values must fill the exact bounded rectangle")
        proposal["before"] = client.range(data, store.settings.document_max_range_cells)
        # Bind the revision after the range read as well; reject a changed file.
        if client.metadata(data.file_id) != snapshot:
            raise Conflict("the file changed while preparing the edit; review it again")
    return proposal


def _replacement(store: DocumentStore, project_id: UUID, row: dict[str, Any], data: ReplaceFile) -> bytes:
    saved = _input(store, project_id, row["work_order_id"], data.input_file_id)
    expected = row["proposal"]["input"]
    if saved["sha256"] != expected["sha256"] or saved["byte_size"] != expected["byte_size"]:
        raise Conflict("the replacement bytes changed after approval")
    files = store.gateway.input_file_store
    if files is None:
        raise Unavailable("input-file storage is unavailable")
    return files._bytes().read(project_id, data.input_file_id, expected["sha256"], expected["byte_size"])


def dispatch(store: DocumentStore, project_id: UUID, action_id: UUID) -> dict[str, Any]:
    if store.gateway.actor_id != store.settings.operator_actor_id:
        raise Conflict("only the operator may dispatch an approved document action")
    row = document_action_store.claim(store.gateway, project_id, action_id)
    attempted = False
    try:
        proposal = row["proposal"]
        data = ACTION.validate_python(proposal["action"])
        source, client = access(store, project_id, data.source_id, write=True)
        if (
            source["version"] != proposal["source_version"]
            or source["account_version"] != proposal["account_version"]
            or client.metadata(data.file_id) != proposal["snapshot"]
        ):
            raise Conflict("the folder or file changed after approval; prepare a new proposal")
        if isinstance(data, ReplaceFile):
            content = _replacement(store, project_id, row, data)
            attempted = True
            client.replace(data.file_id, content, proposal["snapshot"]["mime_type"])
            snapshot = client.metadata(data.file_id)
            change = Change(
                data.file_id, snapshot["name"], snapshot["revision"], len(content), snapshot["mime_type"], ""
            )
            _, observed = client.client.download(change, store.settings.document_max_bytes)
            verified = hashlib.sha256(observed).hexdigest() == proposal["input"]["sha256"]
        elif isinstance(data, EditCells):
            if client.range(data, store.settings.document_max_range_cells) != proposal["before"]:
                raise Conflict("the cells changed after approval; prepare a new proposal")
            attempted = True
            client.edit(data, data.values)
            verified = client.range(data, store.settings.document_max_range_cells) == data.values
        elif isinstance(data, ShareFile):
            attempted = True
            client.share(data.file_id, data.email, data.access)
            verified = client.shared(data.file_id, data.email, data.access)
        readback = {"verified": verified, "operation": data.operation, "file_id": data.file_id}
        return document_action_store.finish(
            store.gateway,
            project_id,
            action_id,
            "done" if verified else "unknown",
            None if verified else "readback_mismatch",
            readback,
        )
    except DocumentError as error:
        state = "failed" if not attempted or error.code == "write_rejected" else "unknown"
        return document_action_store.finish(store.gateway, project_id, action_id, state, error.code)
    except (Unavailable, Conflict, InputFileInvalid):
        return document_action_store.finish(
            store.gateway,
            project_id,
            action_id,
            "unknown" if attempted else "failed",
            "readback_failed" if attempted else "proposal_no_longer_available",
        )
