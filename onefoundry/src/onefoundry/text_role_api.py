"""HTTP surface for explicitly synthetic Researcher to GTM text runs."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from onefoundry.text_role_schemas import (
    SyntheticEligibility,
    SyntheticRunOut,
    SyntheticTextRequest,
    TextOutputOut,
)
from onefoundry.tool_gateway import (
    SyntheticPersistenceUnknown,
    SyntheticRunFailure,
    ToolGateway,
)
from onefoundry.workflow_api import execute


def make_text_role_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get(
        "/work-orders/{order_id}/synthetic-text-eligibility",
        response_model=SyntheticEligibility,
    )
    def eligibility(project_id: UUID, order_id: UUID) -> SyntheticEligibility:
        return execute(
            lambda: SyntheticEligibility.model_validate(
                gateway.synthetic_text_eligibility(project_id, order_id)
            )
        )

    @router.post(
        "/work-orders/{order_id}/synthetic-text-run",
        response_model=SyntheticRunOut,
        status_code=201,
    )
    def run_text(
        project_id: UUID,
        order_id: UUID,
        request: SyntheticTextRequest,
    ) -> SyntheticRunOut:
        del request  # The literal fixture field is validated by Pydantic.
        try:
            saved = execute(lambda: gateway.run_synthetic_text(project_id, order_id))
            return SyntheticRunOut.model_validate(saved)
        except SyntheticRunFailure:
            raise HTTPException(502, "scripted SDK run failed") from None
        except SyntheticPersistenceUnknown:
            raise HTTPException(503, "synthetic result persistence uncertain") from None

    @router.get("/runs/{run_id}/text-outputs", response_model=list[TextOutputOut])
    def list_text_outputs(project_id: UUID, run_id: UUID) -> list[TextOutputOut]:
        return execute(
            lambda: [
                TextOutputOut.model_validate(row) for row in gateway.list_text_outputs(project_id, run_id)
            ]
        )

    return router
