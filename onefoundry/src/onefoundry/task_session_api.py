"""Team session reads and explicit legacy-import open, without model execution."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response

from onefoundry.gateway_errors import Conflict
from onefoundry.task_session_admission import TaskSessionAdmission, TeamActivate, TeamCancel, TeamReadiness
from onefoundry.task_session_artifacts import list_team_artifacts
from onefoundry.task_session_cancellation import cancel_order
from onefoundry.task_session_rework import ChildReworkCreate, ChildReworkOut, return_child
from onefoundry.task_session_schemas import (
    SessionContinueCreate,
    SessionEnqueueOut,
    SessionMessageCreate,
    SessionViewOut,
)
from onefoundry.task_session_start import TaskSessionStart, TeamStart
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.task_session_uncertainty import UnknownAcknowledgement, acknowledge
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_task_session_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/work-orders/{order_id}/team-session")

    def require_worker() -> None:
        settings = gateway.live_text_settings
        if settings is None or not settings.team_worker_enabled:
            raise Conflict("team worker is disabled; no agent turn was queued")

    @router.post("/start", response_model=SessionEnqueueOut)
    def start_work(
        project_id: UUID, order_id: UUID, data: TeamStart, response: Response
    ) -> SessionEnqueueOut:
        saved, created = execute(lambda: TaskSessionStart(gateway).start(project_id, order_id, data))
        response.status_code = 202 if created else 200
        return SessionEnqueueOut.model_validate(saved)

    @router.get("/start/{client_message_id}", response_model=SessionEnqueueOut)
    def start_lookup(project_id: UUID, order_id: UUID, client_message_id: UUID) -> SessionEnqueueOut:
        return SessionEnqueueOut.model_validate(
            execute(lambda: TaskSessionStart(gateway).lookup(project_id, order_id, client_message_id))
        )

    @router.post("/sessions/{session_id}/attempts/{attempt_id}/acknowledge")
    def acknowledge_unknown(
        project_id: UUID, order_id: UUID, session_id: UUID, attempt_id: UUID, data: UnknownAcknowledgement
    ) -> dict[str, Any]:
        return execute(lambda: acknowledge(gateway, project_id, order_id, session_id, attempt_id, data))

    @router.post("/cancel")
    def cancel(project_id: UUID, order_id: UUID, data: TeamCancel) -> dict[str, Any]:
        return execute(
            lambda: cancel_order(gateway, project_id, order_id, data.client_action_id, data.expected_version)
        )

    @router.get("/artifacts")
    def artifacts(project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        return execute(lambda: list_team_artifacts(gateway, project_id, order_id))

    @router.get("/readiness", response_model=TeamReadiness)
    def readiness(project_id: UUID, order_id: UUID) -> TeamReadiness:
        return execute(lambda: TaskSessionAdmission(gateway).inspect(project_id, order_id))

    @router.post("/activate", response_model=SessionViewOut)
    def activate(project_id: UUID, order_id: UUID, data: TeamActivate) -> SessionViewOut:
        return SessionViewOut.model_validate(
            execute(
                lambda: TaskSessionAdmission(gateway).activate(project_id, order_id, data.expected_version)
            )
        )

    @router.get("", response_model=SessionViewOut)
    def get(project_id: UUID, order_id: UUID) -> SessionViewOut:
        return SessionViewOut.model_validate(execute(lambda: gateway.get_task_session(project_id, order_id)))

    @router.get("/sessions/{session_id}", response_model=SessionViewOut)
    def get_agent_session(project_id: UUID, order_id: UUID, session_id: UUID) -> SessionViewOut:
        return SessionViewOut.model_validate(
            execute(lambda: gateway.get_task_session_by_id(project_id, order_id, session_id))
        )

    @router.post("/sessions/{session_id}/reconcile")
    def reconcile_saved_actions(project_id: UUID, order_id: UUID, session_id: UUID) -> dict[str, int]:
        # Verify the route's exact project/order scope before reading any effect.
        execute(lambda: gateway.get_task_session_by_id(project_id, order_id, session_id))
        count = execute(
            lambda: TaskSessionTools(gateway, project_id, session_id, UUID(int=0)).reconcile_previous_actions(
                operator=True
            )
        )
        return {"reconciled_actions": count}

    @router.post("", response_model=SessionViewOut)
    def open_session(project_id: UUID, order_id: UUID, response: Response) -> SessionViewOut:
        saved, created = execute(lambda: gateway.open_task_session(project_id, order_id))
        response.status_code = 201 if created else 200
        return SessionViewOut.model_validate(saved)

    @router.post("/messages", response_model=SessionEnqueueOut)
    def send_message(
        project_id: UUID, order_id: UUID, data: SessionMessageCreate, response: Response
    ) -> SessionEnqueueOut:
        execute(require_worker)
        saved, created = execute(lambda: gateway.send_task_session_message(project_id, order_id, data))
        response.status_code = 202 if created else 200
        return SessionEnqueueOut.model_validate(saved)

    @router.post("/continue", response_model=SessionEnqueueOut)
    def continue_work(
        project_id: UUID, order_id: UUID, data: SessionContinueCreate, response: Response
    ) -> SessionEnqueueOut:
        execute(require_worker)
        saved, created = execute(lambda: gateway.continue_task_session(project_id, order_id, data))
        response.status_code = 202 if created else 200
        return SessionEnqueueOut.model_validate(saved)

    @router.post("/sessions/{session_id}/messages", response_model=SessionEnqueueOut)
    def send_to_agent(
        project_id: UUID,
        order_id: UUID,
        session_id: UUID,
        data: SessionMessageCreate,
        response: Response,
    ) -> SessionEnqueueOut:
        execute(require_worker)
        saved, created = execute(
            lambda: gateway.send_task_session_message(project_id, order_id, data, session_id=session_id)
        )
        response.status_code = 202 if created else 200
        return SessionEnqueueOut.model_validate(saved)

    @router.post("/sessions/{session_id}/return", response_model=ChildReworkOut)
    def return_for_rework(
        project_id: UUID,
        order_id: UUID,
        session_id: UUID,
        data: ChildReworkCreate,
        response: Response,
    ) -> ChildReworkOut:
        saved, created = execute(lambda: return_child(gateway, project_id, order_id, session_id, data))
        response.status_code = 202 if created else 200
        return ChildReworkOut.model_validate(saved)

    @router.post("/sessions/{session_id}/continue", response_model=SessionEnqueueOut)
    def continue_agent(
        project_id: UUID,
        order_id: UUID,
        session_id: UUID,
        data: SessionContinueCreate,
        response: Response,
    ) -> SessionEnqueueOut:
        execute(require_worker)
        saved, created = execute(
            lambda: gateway.continue_task_session(project_id, order_id, data, session_id=session_id)
        )
        response.status_code = 202 if created else 200
        return SessionEnqueueOut.model_validate(saved)

    return router
