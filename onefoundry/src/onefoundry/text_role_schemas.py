"""Contracts for offline Researcher to GTM scripted SDK runs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from onefoundry.workflow_schemas import RunOut, StrictModel


class SyntheticTextRequest(StrictModel):
    fixture: Literal["research_to_gtm"]


class SyntheticEligibility(StrictModel):
    eligible: bool
    reason: str | None
    fixture: Literal["research_to_gtm"] = "research_to_gtm"
    researcher_profile_id: UUID | None
    gtm_profile_id: UUID | None


class SourceMemoryOut(StrictModel):
    id: UUID
    project_id: UUID
    scope: Literal["project", "company"]
    text: str
    source_result_id: UUID


class TextOutputOut(StrictModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    profile_id: UUID
    actor_id: str
    role: Literal["Researcher", "GTM"]
    source_kind: Literal["synthetic_scripted_sdk"]
    configured_model: str | None
    text: str
    text_sha256: str
    source_memory_ids: list[UUID]
    source_memories: list[SourceMemoryOut]
    tokens_used: int
    cost_usd: float
    created_at: datetime


class SyntheticRunOut(StrictModel):
    run: RunOut
    outputs: list[TextOutputOut]
