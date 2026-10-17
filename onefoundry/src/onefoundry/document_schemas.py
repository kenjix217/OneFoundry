"""Operator contracts for watched drive folders."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.workflow_schemas import StrictModel, local_text


class DocumentSourceCreate(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    provider: Literal["onedrive", "gdrive"]
    connector_account_id: UUID
    folder_ref: str = Field(min_length=1, max_length=400)

    @field_validator("label", "folder_ref")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)


class DocumentEnabled(StrictModel):
    enabled: bool
    expected_version: int = Field(ge=1)


class DocumentWriteOut(StrictModel):
    id: UUID
    source_id: UUID
    work_order_id: UUID
    run_id: UUID
    approval_id: UUID
    folder_id: str
    file_name: str
    mime_type: str
    content_sha256: str
    state: str
    error_code: str | None
    provider_file_id: str | None
    readback: dict[str, object] | None
    created_at: datetime
    finished_at: datetime | None


class DocumentActionOut(StrictModel):
    id: UUID
    source_id: UUID
    work_order_id: UUID
    run_id: UUID
    approval_id: UUID
    file_id: str
    file_name: str
    operation: Literal["replace_file", "edit_cells", "share_file"]
    state: Literal["pending", "approved", "rejected", "dispatching", "done", "failed", "unknown"]
    error_code: str | None
    readback: dict[str, object] | None
    created_at: datetime
    finished_at: datetime | None


class DocumentSourceOut(StrictModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    label: str
    folder_ref: str
    folder_id: str | None
    folder_name: str | None
    enabled: bool
    status: str
    last_error_code: str | None
    last_probe_at: datetime | None
    last_poll_at: datetime | None
    version: int
    created_at: datetime
