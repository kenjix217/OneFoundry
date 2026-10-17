"""Operator contracts for Microsoft and Google accounts; client secrets are write-only."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from onefoundry.workflow_schemas import StrictModel, local_text

Feature = Literal["mail", "files", "files_write", "sign_out_everywhere"]


class ConnectorCreate(StrictModel):
    provider: Literal["microsoft", "google"]
    label: str = Field(min_length=1, max_length=80)
    client_id: str = Field(pattern=r"^[A-Za-z0-9._-]{8,200}$")
    tenant: str = Field(default="common", pattern=r"^[A-Za-z0-9.-]{1,100}$")
    client_secret: SecretStr | None = Field(default=None, max_length=500)
    features: list[Feature] = Field(min_length=1, max_length=4)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return local_text(value)

    @model_validator(mode="after")
    def writes_need_reads(self) -> "ConnectorCreate":
        if "sign_out_everywhere" in self.features and (
            self.provider != "microsoft" or self.tenant == "consumers"
        ):
            raise ValueError("sign out everywhere requires a Microsoft work or school account")
        if "files_write" in self.features and "files" not in self.features:
            raise ValueError("file writes also need file reading")
        return self


class ConnectorVersion(StrictModel):
    expected_version: int = Field(ge=1)


class SignOutEverywhere(ConnectorVersion):
    request_id: UUID
    confirmation: Literal["sign_out_all_apps_and_browsers"]


class SignOutResult(StrictModel):
    id: UUID
    account_id: UUID
    state: Literal["dispatching", "completed", "failed", "unknown"]
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None


class ConnectorCallback(StrictModel):
    state: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=1, max_length=2000)


class ConnectorOut(StrictModel):
    id: UUID
    provider: str
    label: str
    client_id: str
    tenant: str
    features: list[str]
    status: str
    last_error_code: str | None
    granted_scopes: str
    connected_at: datetime | None
    version: int
    created_at: datetime


class ConnectorStart(StrictModel):
    kind: Literal["device", "redirect"]
    user_code: str | None = None
    verification_uri: str | None = None
    expires_at: int | None = None
    interval: int | None = None
    authorize_url: str | None = None
