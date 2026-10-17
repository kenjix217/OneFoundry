"""Public contracts for the offline Engineer to QA fixture."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from onefoundry.workflow_schemas import RunOut, StrictModel


class CodeLaunch(StrictModel):
    fixture_id: Literal["engineer_qa_v1"]


class CodeEligibility(StrictModel):
    eligible: bool
    reason: str | None
    fixture_id: Literal["engineer_qa_v1"]
    engineer_profile_id: UUID | None
    qa_profile_id: UUID | None


class CodeTest(StrictModel):
    name: str
    status: Literal["passed", "failed"]
    exit_code: int


class CodeArtifact(StrictModel):
    id: UUID
    kind: str
    path: str
    sha256: str
    byte_size: int


class CodeJob(StrictModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    stage: Literal["engineer", "qa", "merge"]
    source_job_id: UUID | None
    approval_id: UUID | None
    status: Literal["queued", "done", "failed", "unknown"]
    reason_code: str | None
    branch: str | None
    commit: str | None
    baseline_commit: str | None
    merged_commit: str | None
    observed_head_commit: str | None
    diff_sha256: str | None
    tests: list[CodeTest]
    artifacts: list[CodeArtifact]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CodeDetail(StrictModel):
    run: RunOut
    jobs: list[CodeJob]
    synthetic_merge_approval_id: UUID | None


class ArtifactPreview(StrictModel):
    kind: str
    sha256: str
    byte_size: int
    text: str


class RunnerArtifactReceipt(StrictModel):
    kind: str
    path: str
    sha256: str
    byte_size: int = Field(ge=0, le=65536)


class RunnerReceipt(StrictModel):
    schema_version: Literal[1]
    job_id: UUID
    project_id: UUID
    run_id: UUID
    fixture_id: Literal["engineer_qa_v1"]
    stage: Literal["engineer", "qa", "merge"]
    status: Literal["done", "failed", "unknown"]
    reason_code: str = Field(max_length=160)
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int = Field(ge=0, le=120000)
    branch: str | None
    commit: str | None
    baseline_commit: str | None
    merged_commit: str | None
    observed_head_commit: str | None
    diff_sha256: str | None
    tests: list[CodeTest] = Field(max_length=8)
    artifacts: list[RunnerArtifactReceipt] = Field(max_length=8)
    tokens_used: Literal[0]
    cost_usd: Literal[0]
