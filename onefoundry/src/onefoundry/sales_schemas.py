"""Local, fixture-only Sales continuation contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from onefoundry.workflow_schemas import StrictModel


class SalesDraftOut(StrictModel):
    id: UUID
    project_id: UUID
    source_run_id: UUID
    run_id: UUID
    approval_id: UUID
    actor_id: str
    recipient: Literal["sales-preview@onefoundry.invalid"]
    subject: str
    body: str
    body_sha256: str
    delivery_status: Literal["pending", "rejected", "unknown", "delivered"]
    sent_at: datetime | None
    created_at: datetime


class SalesEligibility(StrictModel):
    eligible: bool
    reason: str | None
    sales_profile_id: UUID | None
