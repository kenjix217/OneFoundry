"""HTTP surface for local manual work and approval records."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.qa_review_draft import QAReviewDraftCreate, create_from_request, read_from_request
from onefoundry.role_github_pr import Proposal as GitHubPRReadProposal
from onefoundry.role_github_pr import propose as propose_github_pr_read
from onefoundry.tool_gateway import Conflict, NotFound, ToolGateway, Unavailable
from onefoundry.work_order_recovery import edit_unknown_live_order
from onefoundry.workflow_schemas import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalOut,
    ApprovalStatus,
    EventOut,
    FinishCreate,
    HandoffCreate,
    RunDetail,
    RunOut,
    WorkOrderAssign,
    WorkOrderBlock,
    WorkOrderCreate,
    WorkOrderOut,
    WorkOrderReview,
    WorkOrderUnblock,
    WorkOrderUpdate,
)


def execute[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Unavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "database unavailable") from exc


def make_workflow_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/work-orders", response_model=list[WorkOrderOut])
    def list_work_orders(project_id: UUID) -> list[WorkOrderOut]:
        return execute(
            lambda: [WorkOrderOut.model_validate(row) for row in gateway.list_work_orders(project_id)]
        )

    @router.get("/work-orders/{order_id}/activity", response_model=list[EventOut])
    def work_order_activity(project_id: UUID, order_id: UUID) -> list[EventOut]:
        rows = execute(lambda: gateway.list_work_order_events(project_id, order_id))
        return [EventOut.model_validate(row) for row in rows]

    @router.post("/work-orders", response_model=WorkOrderOut, status_code=201)
    def create_work_order(project_id: UUID, data: WorkOrderCreate) -> WorkOrderOut:
        return execute(lambda: WorkOrderOut.model_validate(gateway.create_work_order(project_id, data)))

    @router.put("/work-orders/{order_id}", response_model=WorkOrderOut)
    def edit_work_order(project_id: UUID, order_id: UUID, data: WorkOrderUpdate) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(edit_unknown_live_order(gateway, project_id, order_id, data))
        )

    @router.post("/work-orders/{order_id}/assign", response_model=WorkOrderOut)
    def assign_work_order(project_id: UUID, order_id: UUID, data: WorkOrderAssign) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(gateway.assign_work_order(project_id, order_id, data))
        )

    @router.post("/work-orders/{order_id}/block", response_model=WorkOrderOut)
    def block_work_order(project_id: UUID, order_id: UUID, data: WorkOrderBlock) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(gateway.block_work_order(project_id, order_id, data))
        )

    @router.post("/work-orders/{order_id}/unblock", response_model=WorkOrderOut)
    def unblock_work_order(project_id: UUID, order_id: UUID, data: WorkOrderUnblock) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(gateway.unblock_work_order(project_id, order_id, data))
        )

    @router.post("/work-orders/{order_id}/ready", response_model=WorkOrderOut)
    def ready_work_order(project_id: UUID, order_id: UUID) -> WorkOrderOut:
        return execute(lambda: WorkOrderOut.model_validate(gateway.mark_ready(project_id, order_id)))

    @router.post("/work-orders/{order_id}/accept", response_model=WorkOrderOut)
    def accept_work_order(project_id: UUID, order_id: UUID, data: WorkOrderReview) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(
                gateway.review_work_order(project_id, order_id, "accept", data)
            )
        )

    @router.post("/work-orders/{order_id}/return", response_model=WorkOrderOut)
    def return_work_order(project_id: UUID, order_id: UUID, data: WorkOrderReview) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(
                gateway.review_work_order(project_id, order_id, "return", data)
            )
        )

    @router.post("/work-orders/{order_id}/reopen", response_model=WorkOrderOut)
    def reopen_work_order(project_id: UUID, order_id: UUID, data: WorkOrderReview) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(
                gateway.review_work_order(project_id, order_id, "reopen", data)
            )
        )

    @router.post("/work-orders/{order_id}/runs", response_model=RunOut, status_code=201)
    def start_run(project_id: UUID, order_id: UUID) -> RunOut:
        return execute(lambda: RunOut.model_validate(gateway.start_run(project_id, order_id)))

    @router.get("/runs", response_model=list[RunOut])
    def list_runs(project_id: UUID, query: str = Query(default="", max_length=120)) -> list[RunOut]:
        return execute(lambda: [RunOut.model_validate(row) for row in gateway.list_runs(project_id, query)])

    @router.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(project_id: UUID, run_id: UUID) -> RunDetail:
        return execute(lambda: RunDetail.model_validate(gateway.get_run(project_id, run_id)))

    @router.post(
        "/runs/{run_id}/qa-review-requests/{request_event_id}/draft",
        response_model=WorkOrderOut,
    )
    def create_qa_review_draft(
        project_id: UUID, run_id: UUID, request_event_id: UUID, data: QAReviewDraftCreate
    ) -> WorkOrderOut:
        return execute(
            lambda: WorkOrderOut.model_validate(
                create_from_request(gateway, project_id, run_id, request_event_id, data)
            )
        )

    @router.get(
        "/runs/{run_id}/qa-review-requests/{request_event_id}/draft",
        response_model=WorkOrderOut | None,
    )
    def read_qa_review_draft(project_id: UUID, run_id: UUID, request_event_id: UUID) -> WorkOrderOut | None:
        return execute(
            lambda: (
                WorkOrderOut.model_validate(row)
                if (row := read_from_request(gateway, project_id, run_id, request_event_id)) is not None
                else None
            )
        )

    @router.post("/runs/{run_id}/handoffs", response_model=RunOut)
    def handoff(project_id: UUID, run_id: UUID, data: HandoffCreate) -> RunOut:
        return execute(lambda: RunOut.model_validate(gateway.handoff(project_id, run_id, data)))

    @router.post("/runs/{run_id}/finish", response_model=RunOut)
    def finish(project_id: UUID, run_id: UUID, data: FinishCreate) -> RunOut:
        return execute(lambda: RunOut.model_validate(gateway.finish(project_id, run_id, data)))

    @router.post("/runs/{run_id}/approval-requests", response_model=ApprovalOut, status_code=201)
    def request_approval(project_id: UUID, run_id: UUID, data: ApprovalCreate) -> ApprovalOut:
        return execute(lambda: ApprovalOut.model_validate(gateway.request_approval(project_id, run_id, data)))

    @router.post("/runs/{run_id}/github-pr-read-approvals", status_code=201)
    def request_github_pr_read(
        project_id: UUID, run_id: UUID, data: GitHubPRReadProposal
    ) -> dict[str, UUID | str]:
        return execute(lambda: propose_github_pr_read(gateway, project_id, run_id, data))

    @router.get("/approvals", response_model=list[ApprovalOut])
    def list_approvals(project_id: UUID, status: ApprovalStatus | None = None) -> list[ApprovalOut]:
        return execute(
            lambda: [ApprovalOut.model_validate(row) for row in gateway.list_approvals(project_id, status)]
        )

    @router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
    def decide_approval(project_id: UUID, approval_id: UUID, data: ApprovalDecision) -> ApprovalOut:
        return execute(
            lambda: ApprovalOut.model_validate(gateway.decide_approval(project_id, approval_id, data))
        )

    return router
