"""HTTP surface for offline Sales drafting and local Mailpit readback."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.sales_schemas import SalesDraftOut, SalesEligibility
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_sales_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/runs/{run_id}/sales-eligibility", response_model=SalesEligibility)
    def eligibility(project_id: UUID, run_id: UUID) -> SalesEligibility:
        return execute(lambda: SalesEligibility.model_validate(gateway.sales_eligibility(project_id, run_id)))

    @router.post("/runs/{run_id}/sales-draft", response_model=SalesDraftOut, status_code=201)
    def draft(project_id: UUID, run_id: UUID) -> SalesDraftOut:
        return execute(lambda: SalesDraftOut.model_validate(gateway.create_sales_draft(project_id, run_id)))

    @router.get("/runs/{run_id}/sales-draft", response_model=SalesDraftOut)
    def get_draft(project_id: UUID, run_id: UUID) -> SalesDraftOut:
        return execute(lambda: SalesDraftOut.model_validate(gateway.get_sales_draft(project_id, run_id)))

    return router
