"""Typed pilot subject identity; no universal subject namespace."""

from typing import Literal
from uuid import UUID

from onefoundry.workflow_schemas import StrictModel


class SubjectRef(StrictModel):
    project_id: UUID
    subject_type: Literal["organization"] = "organization"
    subject_id: UUID
