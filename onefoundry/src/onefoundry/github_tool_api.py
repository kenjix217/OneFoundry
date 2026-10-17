"""Operator HTTP boundary for the single admitted GitHub source check."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.source_store import SourceStore
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalOut, StrictModel


class GitHubSnapshotRequest(StrictModel):
    source_id: UUID


class GitHubSnapshotOut(StrictModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    source_id: UUID
    owner: str
    repo: str
    branch: str
    saved_revision: str
    current_revision: str
    matches_saved_revision: bool
    checked_at: datetime


class GitHubStageProposalRequest(StrictModel):
    run_id: UUID
    source_id: UUID


class GitHubFixtureFetchRequest(StrictModel):
    run_id: UUID
    source_id: UUID
    idempotency_key: UUID


class GitHubFixtureFetchProposalOut(StrictModel):
    approval: ApprovalOut
    source: dict[str, str]
    fetch_status: str
    expires_at: datetime


class GitHubFixtureFetchStatusOut(StrictModel):
    approval_id: UUID
    decision: str
    source: dict[str, str]
    source_current: bool
    fetch_status: str
    expires_at: datetime
    expired: bool
    action: str
    idempotency_key: UUID
    mode: str
    attempt_id: UUID | None = None
    receipt_sha256: str | None = None
    source_revision: str | None = None
    source_tree: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    reason_code: str | None = None


class GitHubFixtureSnapshotProposalOut(StrictModel):
    approval: ApprovalOut
    source: dict[str, str]
    snapshot_status: str
    expires_at: datetime


class GitHubFixtureSnapshotStatusOut(StrictModel):
    approval_id: UUID
    decision: str
    source: dict[str, str]
    source_current: bool
    snapshot_status: str
    expires_at: datetime
    expired: bool
    action: str
    idempotency_key: UUID
    mode: str
    attempt_id: UUID | None = None
    receipt_sha256: str | None = None
    source_revision: str | None = None
    source_tree: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    snapshot_path: str | None = None
    snapshot_sha256: str | None = None
    snapshot_byte_size: int | None = None
    reason_code: str | None = None


class GitHubStageProposalOut(StrictModel):
    approval: ApprovalOut
    source: dict[str, str]
    stage_status: str


class GitHubStageStatusOut(StrictModel):
    approval_id: UUID
    decision: str
    source: dict[str, str]
    source_current: bool
    stage_status: str
    mode: str
    attempt_id: UUID | None = None
    receipt_sha256: str | None = None
    source_tree: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    reason_code: str | None = None


def make_github_tool_router(gateway: ToolGateway, source_store: SourceStore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/tools/github")

    @router.post("/fixture-snapshot-v3", response_model=GitHubFixtureSnapshotProposalOut, status_code=201)
    def propose_fixture_snapshot(
        project_id: UUID, data: GitHubFixtureFetchRequest
    ) -> GitHubFixtureSnapshotProposalOut:
        try:
            return GitHubFixtureSnapshotProposalOut.model_validate(
                gateway.propose_github_fixture_snapshot(
                    project_id, data.run_id, data.source_id, data.idempotency_key
                )
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture snapshot proposal state unavailable") from None

    @router.get("/fixture-snapshot-v3/{approval_id}", response_model=GitHubFixtureSnapshotStatusOut)
    def fixture_snapshot_status(project_id: UUID, approval_id: UUID) -> GitHubFixtureSnapshotStatusOut:
        try:
            return GitHubFixtureSnapshotStatusOut.model_validate(
                gateway.github_fixture_snapshot_status(project_id, approval_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture snapshot status unavailable") from None

    @router.post("/fixture-snapshot-v3/{approval_id}/dispatch", response_model=GitHubFixtureSnapshotStatusOut)
    def dispatch_fixture_snapshot(project_id: UUID, approval_id: UUID) -> GitHubFixtureSnapshotStatusOut:
        try:
            return GitHubFixtureSnapshotStatusOut.model_validate(
                gateway.dispatch_github_fixture_snapshot(project_id, approval_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture snapshot dispatch unavailable") from None

    @router.post("/fixture-fetch-v2", response_model=GitHubFixtureFetchProposalOut, status_code=201)
    def propose_fixture_fetch(
        project_id: UUID, data: GitHubFixtureFetchRequest
    ) -> GitHubFixtureFetchProposalOut:
        try:
            return GitHubFixtureFetchProposalOut.model_validate(
                gateway.propose_github_fixture_fetch(
                    project_id, data.run_id, data.source_id, data.idempotency_key
                )
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture fetch proposal state unavailable") from None

    @router.get("/fixture-fetch-v2/{approval_id}", response_model=GitHubFixtureFetchStatusOut)
    def fixture_fetch_status(project_id: UUID, approval_id: UUID) -> GitHubFixtureFetchStatusOut:
        try:
            return GitHubFixtureFetchStatusOut.model_validate(
                gateway.github_fixture_fetch_status(project_id, approval_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture fetch status unavailable") from None

    @router.post("/fixture-fetch-v2/{approval_id}/dispatch", response_model=GitHubFixtureFetchStatusOut)
    def dispatch_fixture_fetch(project_id: UUID, approval_id: UUID) -> GitHubFixtureFetchStatusOut:
        try:
            return GitHubFixtureFetchStatusOut.model_validate(
                gateway.dispatch_github_fixture_fetch(project_id, approval_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "fixture fetch dispatch unavailable") from None

    @router.post("/stage-proposals", response_model=GitHubStageProposalOut, status_code=201)
    def propose_stage(project_id: UUID, data: GitHubStageProposalRequest) -> GitHubStageProposalOut:
        try:
            return GitHubStageProposalOut.model_validate(
                gateway.propose_github_stage(project_id, data.run_id, data.source_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "GitHub stage proposal state unavailable") from None

    @router.get("/stage-proposals/{approval_id}", response_model=GitHubStageStatusOut)
    def stage_status(project_id: UUID, approval_id: UUID) -> GitHubStageStatusOut:
        try:
            return GitHubStageStatusOut.model_validate(gateway.github_stage_status(project_id, approval_id))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "GitHub stage proposal state unavailable") from None

    @router.post("/stage-proposals/{approval_id}/dispatch", response_model=GitHubStageStatusOut)
    def dispatch_stage(project_id: UUID, approval_id: UUID) -> GitHubStageStatusOut:
        try:
            return GitHubStageStatusOut.model_validate(gateway.dispatch_github_stage(project_id, approval_id))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "offline GitHub stage dispatch state unavailable") from None

    @router.post("/source-snapshot", response_model=GitHubSnapshotOut)
    def source_snapshot(project_id: UUID, data: GitHubSnapshotRequest) -> GitHubSnapshotOut:
        try:
            return GitHubSnapshotOut.model_validate(
                gateway.check_github_source_snapshot(source_store, project_id, data.source_id)
            )
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "GitHub source check state unavailable") from None

    return router
