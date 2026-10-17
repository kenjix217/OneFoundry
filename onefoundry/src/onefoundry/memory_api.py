"""HTTP surface for local results, DreamCycle review, and memory retrieval."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from onefoundry.company_memory_schemas import CaptureCreate, CaptureOut, CaptureSourceOut, MemoryTransition
from onefoundry.memory_schemas import (
    CandidateOut,
    CandidateReview,
    CandidateStatus,
    MemoryOut,
    MemoryScope,
    ResultCreate,
    ResultOut,
    validate_query,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_memory_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/memory/captures", response_model=CaptureOut, status_code=201)
    def capture(project_id: UUID, data: CaptureCreate) -> CaptureOut:
        return execute(lambda: CaptureOut.model_validate(gateway.capture_company_memory(project_id, data)))

    @router.get("/memory/history", response_model=list[MemoryOut])
    def history(project_id: UUID, limit: int = Query(default=20, ge=1, le=50)) -> list[MemoryOut]:
        return execute(
            lambda: [MemoryOut.model_validate(row) for row in gateway.memory_history(project_id, limit)]
        )

    @router.get("/memory/captures/{source_id}", response_model=CaptureSourceOut)
    def source(project_id: UUID, source_id: UUID) -> CaptureSourceOut:
        return execute(
            lambda: CaptureSourceOut.model_validate(gateway.read_memory_source(project_id, source_id))
        )

    @router.post("/memory/{memory_id}/state", response_model=MemoryOut)
    def transition(project_id: UUID, memory_id: UUID, data: MemoryTransition) -> MemoryOut:
        return execute(
            lambda: MemoryOut.model_validate(gateway.transition_memory(project_id, memory_id, data))
        )

    @router.get("/memory/{memory_id}", response_model=MemoryOut)
    def read(project_id: UUID, memory_id: UUID, include_history: bool = False) -> MemoryOut:
        return execute(
            lambda: MemoryOut.model_validate(
                gateway.read_memory(project_id, memory_id, include_history=include_history)
            )
        )

    @router.get("/results", response_model=list[ResultOut])
    def list_results(project_id: UUID) -> list[ResultOut]:
        return execute(lambda: [ResultOut.model_validate(row) for row in gateway.list_results(project_id)])

    @router.post("/runs/{run_id}/results", response_model=ResultOut, status_code=201)
    def create_result(project_id: UUID, run_id: UUID, data: ResultCreate) -> ResultOut:
        return execute(lambda: ResultOut.model_validate(gateway.create_result(project_id, run_id, data)))

    @router.get("/candidates", response_model=list[CandidateOut])
    def list_candidates(project_id: UUID, status: CandidateStatus | None = None) -> list[CandidateOut]:
        return execute(
            lambda: [CandidateOut.model_validate(row) for row in gateway.list_candidates(project_id, status)]
        )

    @router.post("/results/{result_id}/candidates", response_model=CandidateOut, status_code=201)
    def create_candidate(project_id: UUID, result_id: UUID) -> CandidateOut:
        return execute(lambda: CandidateOut.model_validate(gateway.create_candidate(project_id, result_id)))

    @router.post("/candidates/{candidate_id}/review", response_model=CandidateOut)
    def review_candidate(project_id: UUID, candidate_id: UUID, data: CandidateReview) -> CandidateOut:
        return execute(
            lambda: CandidateOut.model_validate(gateway.review_candidate(project_id, candidate_id, data))
        )

    @router.get("/memory", response_model=list[MemoryOut])
    def retrieve_memory(
        project_id: UUID,
        query: str = Query(default="", max_length=120),
        limit: int = Query(default=20, ge=1, le=50),
        scope: MemoryScope | None = None,
    ) -> list[MemoryOut]:
        try:
            checked_query = validate_query(query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return execute(
            lambda: [
                MemoryOut.model_validate(row)
                for row in gateway.retrieve_memory(project_id, checked_query, limit, scope)
            ]
        )

    return router
