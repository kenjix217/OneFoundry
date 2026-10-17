"""Operator initiated live text run; no implicit provider calls on reads."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from onefoundry.live_text_schemas import LiveTextOutputOut, LiveTextRestart, LiveTextRunOut
from onefoundry.live_text_store import LiveTextFailure, LiveTextUnknown
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def _run_response(action: Callable[[], dict[str, Any]]) -> LiveTextRunOut | JSONResponse:
    try:
        return LiveTextRunOut.model_validate(execute(action))
    except LiveTextFailure as exc:
        return JSONResponse(
            status_code=502, content={"detail": "live inference failed", "run_id": str(exc.run_id)}
        )
    except LiveTextUnknown as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": "live inference outcome unknown", "run_id": str(exc.run_id)},
        )


def make_live_text_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/work-orders/{order_id}/live-text-run", response_model=LiveTextRunOut, status_code=201)
    def run(project_id: UUID, order_id: UUID) -> LiveTextRunOut | JSONResponse:
        return _run_response(lambda: gateway.run_live_text(project_id, order_id))

    @router.post("/work-orders/{order_id}/live-text-restart", response_model=LiveTextRunOut, status_code=201)
    def restart(project_id: UUID, order_id: UUID, data: LiveTextRestart) -> LiveTextRunOut | JSONResponse:
        return _run_response(lambda: gateway.run_live_text(project_id, order_id, data))

    @router.get("/runs/{run_id}/live-text-output", response_model=LiveTextOutputOut)
    def output(project_id: UUID, run_id: UUID) -> LiveTextOutputOut:
        return LiveTextOutputOut.model_validate(execute(lambda: gateway.live_text_output(project_id, run_id)))

    return router
