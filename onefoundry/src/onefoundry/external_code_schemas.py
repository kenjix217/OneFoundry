"""Bounded public contract for delegated coding attempts."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from onefoundry.action_trace_contract import ProviderProgressEntry
from onefoundry.github_snapshot_codex_contract import SnapshotCodeSource
from onefoundry.workflow_schemas import RunOut, StrictModel

AdapterId = Literal["codex_cli", "claude_code"]
AttemptAdapterId = Literal["codex_cli", "claude_code", "offline_probe"]
TargetId = Literal[
    "engineer_qa_v1", "local_source_probe_v1", "github_snapshot_codex_fixture_v1", "repository_code_v1"
]


class ExternalCodeLaunch(StrictModel):
    adapter_id: AdapterId


class OfflineSourceProbeLaunch(StrictModel):
    source_id: UUID


class SnapshotCodexFixtureLaunch(StrictModel):
    snapshot_attempt_id: UUID
    idempotency_key: UUID


class SnapshotCodexFixtureChoice(StrictModel):
    snapshot_attempt_id: UUID
    approval_id: UUID
    source_id: UUID
    account_id: UUID
    owner: str
    repo: str
    branch: str
    saved_revision: str
    source_tree: str
    snapshot_path: str
    snapshot_sha256: str
    snapshot_byte_size: int
    receipt_sha256: str


class SnapshotCodexFixtureEligibility(StrictModel):
    eligible: bool
    reason: str | None
    snapshots: list[SnapshotCodexFixtureChoice]


class OfflineSourceProbeEligibility(StrictModel):
    eligible: bool
    reason: str | None


class AdapterEligibility(StrictModel):
    adapter_id: AdapterId
    available: bool
    reason: str | None


class ExternalCodeEligibility(StrictModel):
    eligible: bool
    reason: str | None
    adapters: list[AdapterEligibility]


class ExternalCodeProgressItem(ProviderProgressEntry):
    sequence: int = Field(ge=1, le=24)


class ExternalCodeProgress(StrictModel):
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    coverage: Literal["partial"]
    latest_sequence: int = Field(ge=0, le=24)
    terminal: bool
    items: list[ExternalCodeProgressItem] = Field(max_length=24)


class ExternalCodeArtifact(StrictModel):
    id: UUID
    kind: Literal["diff", "log", "trace"]
    path: str
    sha256: str
    byte_size: int


class ExternalCodeTest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    status: Literal["passed", "failed"]
    exit_code: int


class ExternalCodeAttempt(StrictModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    adapter_id: AttemptAdapterId
    fixture_id: TargetId
    target_id: TargetId
    source_id: UUID | None
    source_root_id: str | None
    source_relative_path: str | None
    source_branch: str | None
    source_revision: str | None
    snapshot_attempt_id: UUID | None
    idempotency_key: UUID | None
    intent_sha256: str | None
    snapshot_source_id: UUID | None
    snapshot_account_id: UUID | None
    snapshot_owner: str | None
    snapshot_repo: str | None
    snapshot_branch: str | None
    snapshot_revision: str | None
    snapshot_tree: str | None
    snapshot_path: str | None
    snapshot_sha256: str | None
    snapshot_byte_size: int | None
    snapshot_request_sha256: str | None
    snapshot_receipt_sha256: str | None
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["queued", "done", "failed", "unknown"]
    reason_code: str | None
    provider_session_id: str | None
    baseline_commit: str | None
    branch: str | None
    commit: str | None
    diff_sha256: str | None
    tests: list[ExternalCodeTest]
    tokens_used: int | None
    cost_usd: Decimal | None
    receipt_sha256: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    artifacts: list[ExternalCodeArtifact]


class ExternalCodeDetail(StrictModel):
    run: RunOut
    attempts: list[ExternalCodeAttempt]


class ExternalCodeArtifactPreview(StrictModel):
    kind: Literal["diff", "log", "trace"]
    sha256: str
    byte_size: int
    text: str


class ExternalCodeReceiptArtifact(StrictModel):
    kind: Literal["diff", "log", "trace"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0, le=65536)


class ExternalCodeSourceSnapshot(StrictModel):
    source_id: UUID
    root_id: Literal["workspace"]
    relative_path: str = Field(min_length=1, max_length=500)
    branch: str = Field(min_length=1, max_length=200)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class ExternalCodeReceipt(StrictModel):
    schema_version: Literal[1, 2, 3, 4]
    job_id: UUID
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    adapter_id: AttemptAdapterId
    fixture_id: Literal["engineer_qa_v1"] | None = None
    target_id: (
        Literal["local_source_probe_v1", "github_snapshot_codex_fixture_v1", "repository_code_v1"] | None
    ) = None
    source: ExternalCodeSourceSnapshot | None = None
    snapshot: SnapshotCodeSource | None = None
    idempotency_key: UUID | None = None
    intent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["done", "failed", "unknown"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,159}$")
    started_at: datetime
    finished_at: datetime
    provider_session_id: str | None = Field(default=None, max_length=200)
    baseline_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    branch: str | None = Field(default=None, max_length=160)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifacts: list[ExternalCodeReceiptArtifact] = Field(max_length=3)
    tests: list[ExternalCodeTest] = Field(default_factory=list, max_length=16)
    tokens_used: int | None = Field(default=None, ge=0, le=2_147_483_647)
    cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def target_matches_version(self) -> "ExternalCodeReceipt":
        if self.schema_version != 4 and len(self.tests) > 8:
            raise ValueError("legacy coding receipt exceeds its test bound")
        if self.schema_version == 1:
            if (
                self.adapter_id == "offline_probe"
                or self.fixture_id != "engineer_qa_v1"
                or self.target_id is not None
                or self.source is not None
                or self.snapshot is not None
                or self.idempotency_key is not None
                or self.intent_sha256 is not None
            ):
                raise ValueError("v1 receipt requires only the fixture target")
        elif self.schema_version == 2 and (
            self.adapter_id != "offline_probe"
            or self.fixture_id is not None
            or self.target_id != "local_source_probe_v1"
            or self.source is None
            or self.snapshot is not None
            or self.idempotency_key is not None
            or self.intent_sha256 is not None
        ):
            raise ValueError("v2 receipt requires the local source target and snapshot")
        elif self.schema_version == 3 and (
            self.adapter_id != "codex_cli"
            or self.fixture_id is not None
            or self.target_id != "github_snapshot_codex_fixture_v1"
            or self.source is not None
            or self.snapshot is None
            or self.idempotency_key is None
            or self.intent_sha256 is None
        ):
            raise ValueError("v3 receipt requires the synthetic GitHub snapshot target")
        elif self.schema_version == 4 and (
            self.adapter_id not in ("codex_cli", "claude_code")
            or self.fixture_id is not None
            or self.target_id != "repository_code_v1"
            or self.source is None
            or self.snapshot is not None
            or self.idempotency_key is None
            or self.intent_sha256 is None
        ):
            raise ValueError("v4 receipt requires an exact approved maintenance target")
        return self
