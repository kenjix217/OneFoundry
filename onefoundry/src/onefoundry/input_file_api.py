"""Bounded, owner-scoped binary upload and metadata readback."""

from datetime import datetime
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.input_file_bytes import InputFileAlreadyExists, InputFileInvalid, InputFileTooLarge
from onefoundry.input_file_store import InputFileStore
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class InputFileOut(StrictModel):
    id: UUID
    project_id: UUID
    display_name: str
    extension: str
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1)
    status: str
    reason_code: str | None
    extractor_version: str | None
    created_at: datetime
    finished_at: datetime | None


def make_input_file_router(store: InputFileStore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/input-files")

    @router.post("", response_model=InputFileOut, status_code=201)
    async def upload(
        project_id: UUID,
        request: Request,
        scope: str = Query(default="project", pattern=r"^(project|work_order|staged)$"),
        work_order_id: UUID | None = None,
    ) -> InputFileOut:
        raw_name = request.headers.get("x-file-name", "")
        if len(raw_name) > 768:
            raise HTTPException(422, "file name is too long")
        name = unquote(raw_name)
        raw_id = request.headers.get("x-client-file-id")
        try:
            file_id = UUID(raw_id) if raw_id else None
        except ValueError as exc:
            raise HTTPException(422, "client file ID is invalid") from exc
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "content length is invalid") from exc
            if length > store.settings.input_file_max_bytes:
                raise HTTPException(413, "input file exceeds the configured limit")
        try:
            row = await store.upload(
                project_id,
                name,
                request.stream(),
                scope=scope,
                work_order_id=work_order_id,
                file_id=file_id,
            )
            return InputFileOut.model_validate(row)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc
        except InputFileTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except InputFileAlreadyExists as exc:
            raise HTTPException(409, str(exc)) from exc
        except InputFileInvalid as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("", response_model=list[InputFileOut])
    def list_files(project_id: UUID, work_order_id: UUID | None = None) -> list[InputFileOut]:
        return [
            InputFileOut.model_validate(row) for row in execute(lambda: store.list(project_id, work_order_id))
        ]

    @router.get("/{file_id}", response_model=InputFileOut)
    def get(project_id: UUID, file_id: UUID) -> InputFileOut:
        return InputFileOut.model_validate(execute(lambda: store.get(project_id, file_id)))

    return router
