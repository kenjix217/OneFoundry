"""HTTP entrypoints for the bounded local coding fixture."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.synthetic_code_schemas import (
    ArtifactPreview,
    CodeDetail,
    CodeEligibility,
    CodeLaunch,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import ApprovalOut


def make_synthetic_code_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/work-orders/{order_id}/synthetic-code/eligibility", response_model=CodeEligibility)
    def eligibility(project_id: UUID, order_id: UUID) -> CodeEligibility:
        return execute(
            lambda: CodeEligibility.model_validate(gateway.synthetic_code_eligibility(project_id, order_id))
        )

    @router.post("/work-orders/{order_id}/synthetic-code/runs", response_model=CodeDetail, status_code=201)
    def launch(project_id: UUID, order_id: UUID, request: CodeLaunch) -> CodeDetail:
        del request
        return execute(lambda: CodeDetail.model_validate(gateway.launch_synthetic_code(project_id, order_id)))

    @router.get("/runs/{run_id}/synthetic-code", response_model=CodeDetail)
    def detail(project_id: UUID, run_id: UUID) -> CodeDetail:
        return execute(lambda: CodeDetail.model_validate(gateway.synthetic_code_detail(project_id, run_id)))

    @router.post("/runs/{run_id}/synthetic-code/reconcile", response_model=CodeDetail)
    def reconcile(project_id: UUID, run_id: UUID) -> CodeDetail:
        return execute(
            lambda: CodeDetail.model_validate(gateway.reconcile_synthetic_code(project_id, run_id))
        )

    @router.get("/runs/{run_id}/synthetic-code/artifacts/{artifact_id}", response_model=ArtifactPreview)
    def preview(project_id: UUID, run_id: UUID, artifact_id: UUID) -> ArtifactPreview:
        return execute(
            lambda: ArtifactPreview.model_validate(
                gateway.synthetic_code_artifact(project_id, run_id, artifact_id)
            )
        )

    @router.post("/runs/{run_id}/synthetic-code/merge-approval", response_model=ApprovalOut, status_code=201)
    def merge_approval(project_id: UUID, run_id: UUID) -> ApprovalOut:
        return execute(
            lambda: ApprovalOut.model_validate(gateway.request_synthetic_merge(project_id, run_id))
        )

    return router
