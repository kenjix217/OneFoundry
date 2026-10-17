"""Thin operator HTTP routes for inbox-bound private static fixtures."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from onefoundry.browser_static_fixtures import StaticFixtureInput
from onefoundry.workflow_api import execute


class FixtureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    run_id: UUID
    approval_id: UUID
    actor_id: str
    approved_url: str
    source_sha256: str
    proposal_digest: str
    viewport_width: int
    viewport_height: int
    max_page_bytes: int
    timeout_seconds: int
    lifetime_seconds: int
    status: str
    created_at: datetime
    decided_at: datetime | None


class BrowserFixtureGateway(Protocol):
    def create_browser_static_fixture(
        self, project_id: UUID, run_id: UUID, data: StaticFixtureInput
    ) -> dict[str, Any]: ...

    def get_browser_static_fixture(
        self, project_id: UUID, run_id: UUID, fixture_id: UUID
    ) -> dict[str, Any]: ...


def make_browser_static_fixture_router(gateway: BrowserFixtureGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/runs/{run_id}/browser-static-fixtures")

    @router.post("", response_model=FixtureSummary, status_code=201)
    def propose(project_id: UUID, run_id: UUID, data: StaticFixtureInput) -> FixtureSummary:
        return execute(
            lambda: FixtureSummary.model_validate(
                gateway.create_browser_static_fixture(project_id, run_id, data)
            )
        )

    @router.get("/{fixture_id}", response_model=FixtureSummary)
    def read(project_id: UUID, run_id: UUID, fixture_id: UUID) -> FixtureSummary:
        return execute(
            lambda: FixtureSummary.model_validate(
                gateway.get_browser_static_fixture(project_id, run_id, fixture_id)
            )
        )

    return router
