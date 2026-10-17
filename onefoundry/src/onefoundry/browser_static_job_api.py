"""Operator readback for approved static browser jobs and their receipts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


class BrowserJobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    run_id: UUID
    fixture_id: UUID
    actor_id: str
    status: str
    reason_code: str | None
    request_sha256: str
    binding_digest: str
    source_sha256: str
    image_id: str
    receipt_sha256: str | None
    created_at: datetime
    finished_at: datetime | None


class BrowserJobEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    project_id: UUID
    run_id: UUID
    status: str
    title: str
    text: str
    blocked_request_count: int
    receipt_sha256: str
    finished_at: datetime


class BrowserJobDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: UUID
    idempotency_key: UUID


def make_browser_static_job_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/runs/{run_id}/browser-static-jobs")

    @router.post("", response_model=BrowserJobSummary, status_code=status.HTTP_201_CREATED)
    def dispatch(project_id: UUID, run_id: UUID, body: BrowserJobDispatch) -> BrowserJobSummary:
        def apply() -> BrowserJobSummary:
            jobs = gateway._browser_jobs()
            row = jobs.launch_for_assigned_role(project_id, run_id, body.fixture_id, body.idempotency_key)
            return BrowserJobSummary.model_validate(jobs.get(project_id, run_id, row["id"]))

        return execute(apply)

    @router.get("/by-key/{idempotency_key}", response_model=BrowserJobSummary)
    def by_key(project_id: UUID, run_id: UUID, idempotency_key: UUID) -> BrowserJobSummary:
        def read() -> BrowserJobSummary:
            row = gateway._browser_jobs().by_idempotency_key(project_id, run_id, idempotency_key)
            if row is None:
                raise HTTPException(status_code=404, detail="browser job not found")
            return BrowserJobSummary.model_validate(
                gateway._browser_jobs().get(project_id, run_id, row["id"])
            )

        return execute(read)

    @router.get("", response_model=list[BrowserJobSummary])
    def list_jobs(project_id: UUID, run_id: UUID) -> list[BrowserJobSummary]:
        return execute(
            lambda: [
                BrowserJobSummary.model_validate(row)
                for row in gateway._browser_jobs().list_for_run(project_id, run_id)
            ]
        )

    @router.get("/{job_id}", response_model=BrowserJobSummary)
    def get_job(project_id: UUID, run_id: UUID, job_id: UUID) -> BrowserJobSummary:
        return execute(
            lambda: BrowserJobSummary.model_validate(gateway._browser_jobs().get(project_id, run_id, job_id))
        )

    @router.post("/{job_id}/reconcile", response_model=BrowserJobSummary)
    def reconcile(project_id: UUID, run_id: UUID, job_id: UUID) -> BrowserJobSummary:
        def apply() -> BrowserJobSummary:
            gateway._browser_jobs().reconcile(project_id, run_id, job_id)
            return BrowserJobSummary.model_validate(gateway._browser_jobs().get(project_id, run_id, job_id))

        return execute(apply)

    @router.get("/{job_id}/evidence", response_model=BrowserJobEvidence)
    def evidence(project_id: UUID, run_id: UUID, job_id: UUID) -> BrowserJobEvidence:
        return execute(
            lambda: BrowserJobEvidence.model_validate(
                gateway._browser_jobs().evidence(project_id, run_id, job_id)
            )
        )

    return router
