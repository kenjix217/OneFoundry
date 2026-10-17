"""Operator action to stage a completed synthetic result for DreamCycle review."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.memory_schemas import SyntheticProposalCreate, SyntheticProposalOut
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_synthetic_memory_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post(
        "/runs/{run_id}/synthetic-memory-candidate",
        response_model=SyntheticProposalOut,
        status_code=201,
    )
    def propose(project_id: UUID, run_id: UUID, data: SyntheticProposalCreate) -> SyntheticProposalOut:
        def action() -> SyntheticProposalOut:
            result, candidate = gateway.propose_synthetic_candidate(project_id, run_id, data.scope)
            return SyntheticProposalOut.model_validate({"result": result, "candidate": candidate})

        return execute(action)

    return router
