"""Operator HTTP routes for watched drive folders."""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from onefoundry import document_action_store, document_operations, document_writes
from onefoundry.document_action_schemas import RangeRead, Search
from onefoundry.document_api_sources import DocumentError
from onefoundry.document_schemas import (
    DocumentActionOut,
    DocumentEnabled,
    DocumentSourceCreate,
    DocumentSourceOut,
    DocumentWriteOut,
)
from onefoundry.document_store import DocumentStore
from onefoundry.gateway_errors import Unavailable
from onefoundry.workflow_api import execute


def make_document_router(store: DocumentStore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/documents")

    @router.get("/actions", response_model=list[DocumentActionOut])
    def list_actions(project_id: UUID) -> list[DocumentActionOut]:
        return execute(
            lambda: [
                DocumentActionOut.model_validate(row)
                for row in document_action_store.list_actions(store.gateway, project_id)
            ]
        )

    @router.post("/actions/{action_id}/dispatch", response_model=DocumentActionOut)
    def dispatch_action(project_id: UUID, action_id: UUID) -> DocumentActionOut:
        return execute(
            lambda: DocumentActionOut.model_validate(
                document_operations.dispatch(store, project_id, action_id)
            )
        )

    @router.post("/search")
    def search(project_id: UUID, data: Search) -> dict[str, object]:
        def read() -> dict[str, object]:
            try:
                return document_operations.search(store, project_id, data)
            except DocumentError as error:
                raise Unavailable(f"folder search failed: {error.code}") from None

        return execute(read)

    @router.post("/range")
    def read_range(project_id: UUID, data: RangeRead) -> dict[str, object]:
        def read() -> dict[str, object]:
            try:
                return document_operations.read_range(store, project_id, data)
            except DocumentError as error:
                raise Unavailable(f"range read failed: {error.code}") from None

        return execute(read)

    @router.get("/writes", response_model=list[DocumentWriteOut])
    def list_writes(project_id: UUID) -> list[DocumentWriteOut]:
        return execute(
            lambda: [
                DocumentWriteOut.model_validate(row)
                for row in document_writes.list_writes(store.gateway, project_id)
            ]
        )

    @router.post("/writes/{write_id}/dispatch", response_model=DocumentWriteOut)
    def dispatch(project_id: UUID, write_id: UUID) -> DocumentWriteOut:
        return execute(
            lambda: DocumentWriteOut.model_validate(
                document_writes.dispatch(store.gateway, store, project_id, write_id)
            )
        )

    @router.get("/sources", response_model=list[DocumentSourceOut])
    def list_sources(project_id: UUID) -> list[DocumentSourceOut]:
        return execute(lambda: [DocumentSourceOut.model_validate(row) for row in store.list(project_id)])

    @router.post("/sources", response_model=DocumentSourceOut, status_code=201)
    def create(project_id: UUID, data: DocumentSourceCreate) -> DocumentSourceOut:
        return execute(lambda: DocumentSourceOut.model_validate(store.create(project_id, data)))

    @router.post("/sources/{source_id}/probe", response_model=DocumentSourceOut)
    def probe(project_id: UUID, source_id: UUID) -> DocumentSourceOut:
        return execute(lambda: DocumentSourceOut.model_validate(store.probe(project_id, source_id)))

    @router.post("/sources/{source_id}/enabled", response_model=DocumentSourceOut)
    def set_enabled(project_id: UUID, source_id: UUID, data: DocumentEnabled) -> DocumentSourceOut:
        return execute(
            lambda: DocumentSourceOut.model_validate(
                store.set_enabled(project_id, source_id, data.enabled, data.expected_version)
            )
        )

    @router.delete("/sources/{source_id}", status_code=204)
    def delete(project_id: UUID, source_id: UUID, expected_version: int = Query(ge=1)) -> Response:
        execute(lambda: store.delete(project_id, source_id, expected_version))
        return Response(status_code=204)

    return router
