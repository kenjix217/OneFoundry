"""Scoped preflight observations and actionable blockers; never access grants."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.workflow_schemas import StrictModel

BlockerKind = Literal[
    "disabled",
    "permission_required",
    "configuration_missing",
    "dependency_unavailable",
    "missing_implementation",
    "unknown",
]
CapabilityStatus = Literal[
    "ready",
    "disabled",
    "permission_required",
    "configuration_missing",
    "dependency_unavailable",
    "missing_implementation",
    "unknown",
]
NextAction = Literal["use", "configure", "grant_access", "reconcile", "implement"]
PRIORITY: tuple[BlockerKind, ...] = (
    "disabled",
    "permission_required",
    "configuration_missing",
    "dependency_unavailable",
    "unknown",
    "missing_implementation",
)


class CapabilityBlocker(StrictModel):
    kind: BlockerKind
    code: str = Field(min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)
    next_step: str = Field(min_length=1, max_length=300)


class CapabilityCheck(StrictModel):
    operation: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=80)
    source_id: UUID | None = None
    source_name: str | None = Field(default=None, max_length=500)
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    status: CapabilityStatus
    next_action: NextAction
    blockers: list[CapabilityBlocker] = Field(max_length=8)

    @model_validator(mode="after")
    def consistent_action(self) -> "CapabilityCheck":
        status, action = resolution(self.blockers)
        if (self.status, self.next_action) != (status, action):
            raise ValueError("capability status and action must match observed blockers")
        return self


class CapabilityReport(StrictModel):
    project_id: UUID
    profile_id: UUID | None = None
    observed_at: datetime
    checks: list[CapabilityCheck] = Field(max_length=50)
    omitted_sources: int = Field(default=0, ge=0)
    boundary: str = (
        "Preflight observations are not tool grants or successful execution. Owners recheck at use."
    )

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observation time requires a timezone")
        return value


def resolution(blockers: list[CapabilityBlocker]) -> tuple[CapabilityStatus, NextAction]:
    for kind in PRIORITY:
        if any(item.kind == kind for item in blockers):
            if kind == "permission_required":
                return kind, "grant_access"
            if kind == "unknown":
                return kind, "reconcile"
            if kind == "missing_implementation":
                return kind, "implement"
            return kind, "configure"
    return "ready", "use"


def capability_check(
    operation: str,
    title: str,
    owner: str,
    blockers: list[CapabilityBlocker],
    *,
    source_id: UUID | None = None,
    source_name: str | None = None,
    revision: str | None = None,
) -> CapabilityCheck:
    status, action = resolution(blockers)
    return CapabilityCheck(
        operation=operation,
        title=title,
        owner=owner,
        source_id=source_id,
        source_name=source_name,
        revision=revision,
        status=status,
        next_action=action,
        blockers=blockers,
    )
