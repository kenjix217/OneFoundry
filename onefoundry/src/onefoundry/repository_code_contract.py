"""Exact maintenance-target contracts shared by Gateway and separate runner."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.role_repository_file import _valid_path
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel, local_text

TARGET: Literal["repository_code_v1"] = "repository_code_v1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def exact_provider(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ValueError("maintenance provider must be an exact credential-free HTTPS endpoint")
    return value


class RepositoryCodePolicy(StrictModel):
    """Bounds are configuration, not model-selected overrides."""

    max_files: int = Field(default=10000, ge=1, le=100000)
    max_bytes: int = Field(default=67108864, ge=1024, le=268435456)
    max_tree_bytes: int = Field(default=4194304, ge=1024, le=16777216)
    stage_seconds: int = Field(default=30, ge=1, le=120)
    wall_seconds: int = Field(default=900, ge=30, le=3600)
    receipt_wait_seconds: int = Field(default=1800, ge=120, le=7200)
    max_context_file_bytes: int = Field(default=262144, ge=1024, le=1048576)
    max_provider_requests: int = Field(default=80, ge=1, le=100)
    max_provider_request_bytes: int = Field(default=1000000, ge=1024, le=4000000)
    max_provider_response_bytes: int = Field(default=4000000, ge=1024, le=16000000)
    max_provider_output_tokens: int = Field(default=16000, ge=1, le=64000)
    maintenance_lock_seconds: int = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def enough_reconciliation_time(self) -> RepositoryCodePolicy:
        if self.receipt_wait_seconds < self.wall_seconds + self.stage_seconds + 60:
            raise ValueError("maintenance receipt wait must cover staging, execution and reconciliation")
        return self


class RepositoryCodeTarget(StrictModel):
    kind: Literal["repository_code_target_v1"] = "repository_code_target_v1"
    source_id: UUID
    provider_url: str = Field(min_length=1, max_length=500)
    task_scope_id: UUID
    adapter_id: Literal["codex_cli", "claude_code"]
    max_attempts: Literal[1] = 1
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    test_commands: list[list[str]] = Field(min_length=1, max_length=8)
    test_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}$")
    reasoning_effort: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$", exclude_if=lambda value: value is None
    )
    execution_policy: RepositoryCodePolicy | None = None
    output_limits: dict[str, int] | None = None

    _provider = field_validator("provider_url")(exact_provider)

    @field_validator("allowed_paths")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _valid_path(value) for value in values):
            raise ValueError("maintenance paths must be explicit safe file paths")
        return sorted(values)

    @field_validator("test_commands")
    @classmethod
    def bounded_commands(cls, values: list[list[str]]) -> list[list[str]]:
        if any(
            not 1 <= len(argv) <= 32
            or any(not arg or len(arg) > 500 or any(ord(c) < 32 for c in arg) for arg in argv)
            for argv in values
        ):
            raise ValueError("invalid test argument vector")
        # Commands run only in a networkless disposable container, never the app/host.
        if contains_sensitive_text(json.dumps(values)):
            raise ValueError("test commands appear to contain sensitive data")
        return values


class RepositoryCodeLaunch(StrictModel):
    target_approval_id: UUID
    idempotency_key: UUID
    adapter_id: Literal["codex_cli", "claude_code"]


class RepositoryCodeManifest(StrictModel):
    schema_version: Literal[4] = 4
    target_id: Literal["repository_code_v1"] = TARGET
    job_id: UUID
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    work_order_id: UUID
    actor_id: str
    adapter_id: Literal["codex_cli", "claude_code"]
    model_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}$")
    task_title: str = Field(min_length=1, max_length=160)
    task_instructions: str = Field(min_length=1, max_length=6000)
    target_approval_id: UUID
    target: RepositoryCodeTarget
    source: dict[str, str]
    idempotency_key: UUID
    limits: dict[str, int]
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_limits: RepositoryCodePolicy
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_identity(self) -> RepositoryCodeManifest:
        from runner.local_source_stage import LocalSource

        source = LocalSource.parse(self.source)
        if (
            self.job_id != self.attempt_id
            or source.source_id != self.target.source_id
            or source.revision != self.target.revision
            or self.target.adapter_id != self.adapter_id
            or self.target.model_id != self.model_id
            or self.target.execution_policy != self.stage_limits
            or self.target.output_limits
            != {key: self.limits.get(key) for key in ("max_log_bytes", "max_diff_bytes")}
            or set(self.limits) != {"wall_seconds", "max_log_bytes", "max_diff_bytes"}
            or not 1 <= self.limits["wall_seconds"] <= self.stage_limits.wall_seconds
            or not 512 <= self.limits["max_log_bytes"] <= 65536
            or not 512 <= self.limits["max_diff_bytes"] <= 65536
            or self.request_sha256 != digest(self.model_dump(mode="json", exclude={"request_sha256"}))
        ):
            raise ValueError("repository job identity or bounds changed")
        local_text(self.task_title)
        local_text(self.task_instructions)
        return self


class RepositoryRunnerCapability(StrictModel):
    schema_version: Literal[1] = 1
    mode: Literal["isolated_repository_code_v1"] = "isolated_repository_code_v1"
    observed_at: datetime
    isolated: Literal[True]
    provider_only_egress: Literal[True]
    credentials_separate: Literal[True]
    receipts_separate: Literal[True]
    source_root_ready: Literal[True]
    adapter_id: Literal["codex_cli", "claude_code"]
    model_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}$")
    provider_url: str
    _provider = field_validator("provider_url")(exact_provider)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    qa_image: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("runner observation requires a timezone")
        return value
