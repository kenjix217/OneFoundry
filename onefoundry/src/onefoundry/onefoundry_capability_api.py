"""Read-only OneFoundry capability route."""

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import Engine

from onefoundry.onefoundry_capability_readback import CapabilityReadback, OneFoundryCapabilityReader
from onefoundry.workflow_api import execute


def make_onefoundry_capability_router(engine: Engine) -> APIRouter:
    reader = OneFoundryCapabilityReader(engine)
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/profiles/{profile_id}/onefoundry-capability", response_model=CapabilityReadback)
    def read_capability(project_id: UUID, profile_id: UUID) -> CapabilityReadback:
        return execute(lambda: reader.read_capability(project_id, profile_id))

    return router
